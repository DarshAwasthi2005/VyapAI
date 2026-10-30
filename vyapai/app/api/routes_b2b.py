from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph_b2b import build_b2b_graph
from app.agents.guardrails import validate_ledger_entry_amount
from app.api.routes_auth import scoped_merchant
from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    EntryType,
    LedgerEntry,
    Merchant,
    Order,
    OrderLineItem,
    Payment,
)
from app.db.session import get_db
from app.integrations import media
from app.schemas import LedgerEntryOut, MerchantCreate, MerchantOut, TextBillEntryRequest
from app.services import auth as authsvc
from app.services.accounting import build_purchase_transaction, build_sale_transaction
from app.services.audit import record_audit
from app.services.credit import build_cash_flow_summary, loan_readiness_score

router = APIRouter(prefix="/b2b", tags=["b2b"])
_graph = build_b2b_graph()


def _build(payload: TextBillEntryRequest):
    """Pure transaction builder for the text path — no persistence here."""
    builder = build_sale_transaction if payload.transaction_kind == "sale" else build_purchase_transaction
    return builder(
        taxable_value=payload.taxable_value,
        gst_rate=payload.gst_rate,
        party=payload.party_name,
        source="manual",
    )


# --- Merchant-scoped read endpoints (power the dashboard) -----------------


def _ledger_line(e: LedgerEntry) -> dict:
    return {
        "id": e.id,
        "transaction_group": e.transaction_group,
        "account": e.account,
        "entry_type": e.entry_type.value,
        "amount": float(e.amount),
        "gst_rate": e.gst_rate,
        "source": e.source,
        "memo": e.memo,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/merchants")
def list_merchants(db: Session = Depends(get_db), authorization: str = Header(default="")):
    """Signed-in shop list. Protected: before real auth this endpoint leaked
    every shop's id and name to anyone — it now requires a live session."""
    try:
        authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    return [
        {
            "id": m.id,
            "business_name": m.business_name,
            "preferred_language": m.preferred_language,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in db.query(Merchant).order_by(Merchant.created_at).all()
    ]


@router.get("/merchants/{merchant_id}/ledger")
def get_ledger(merchant_id: str, db: Session = Depends(get_db), authorization: str = Header(default="")):
    scoped_merchant(merchant_id, db, authorization)
    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.merchant_id == merchant_id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(200)
        .all()
    )
    return [_ledger_line(e) for e in entries]


@router.get("/merchants/{merchant_id}/orders")
def get_orders(merchant_id: str, db: Session = Depends(get_db), authorization: str = Header(default="")):
    scoped_merchant(merchant_id, db, authorization)
    orders = (
        db.query(Order)
        .filter(Order.merchant_id == merchant_id)
        .order_by(Order.created_at.desc())
        .all()
    )
    out = []
    for o in orders:
        payment = (
            db.query(Payment).filter(Payment.order_id == o.id).one_or_none()
        )
        out.append(
            {
                "id": o.id,
                "buyer": o.buyer_whatsapp_number,
                "raw_message": o.raw_message,
                "status": o.status.value,
                "requested_by": o.requested_by,
                "total_amount": float(o.total_amount),
                "offer_type": o.offer_type,
                "offer_value": o.offer_value,
                "discount_amount": float(o.discount_amount or 0),
                "subtotal": float((o.total_amount or 0)) + float(o.discount_amount or 0),
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "payment_status": payment.status.value if payment else None,
                "payment_method": payment.method if payment else None,
                "upi_deep_link": payment.upi_deep_link if payment else None,
                "transaction_ref": payment.transaction_ref if payment else None,
                "line_items": [
                    {
                        "item_name": li.item_name,
                        "quantity": li.quantity,
                        "unit": li.unit,
                    }
                    for li in o.line_items
                ],
            }
        )
    return out


@router.get("/merchants/{merchant_id}/approvals")
def get_approvals(merchant_id: str, db: Session = Depends(get_db), authorization: str = Header(default="")):
    scoped_merchant(merchant_id, db, authorization)
    approvals = (
        db.query(ApprovalRequest)
        .filter(ApprovalRequest.merchant_id == merchant_id)
        .order_by(ApprovalRequest.created_at.desc())
        .all()
    )
    return [
        {
            "id": a.id,
            "action_type": a.action_type,
            "status": a.status.value,
            "reason": a.reason,
            "payload": a.payload_json,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in approvals
    ]


@router.post("/merchants", response_model=MerchantOut)
def create_merchant(payload: MerchantCreate, db: Session = Depends(get_db)):
    if not payload.pin:
        raise HTTPException(422, "A login PIN is required to create a shop")
    if not payload.whatsapp_number and not payload.email:
        raise HTTPException(422, "Register with a phone number or an email address — at least one is required")
    if payload.whatsapp_number and db.execute(
        select(Merchant).where(Merchant.whatsapp_number == payload.whatsapp_number)
    ).scalar_one_or_none():
        raise HTTPException(409, "A shop is already registered with this WhatsApp number — sign in instead")
    if payload.email and db.execute(
        select(Merchant).where(func.lower(Merchant.email) == payload.email.strip().lower())
    ).scalar_one_or_none():
        raise HTTPException(409, "A shop is already registered with this email — sign in instead")
    fields = {k: v for k, v in payload.model_dump().items() if k != "pin"}
    if fields.get("email"):
        fields["email"] = fields["email"].strip().lower()
    merchant = Merchant(**fields)
    db.add(merchant)
    try:
        authsvc.set_pin(merchant, payload.pin)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    db.flush()
    record_audit(
        db, action="merchant_registered", object_type="merchant", object_id=merchant.id,
        merchant_id=merchant.id, actor_type="api",
        after={"business_name": merchant.business_name,
               "identity": "email" if fields.get("email") else "phone"},
    )
    return merchant


@router.post("/entries/text", response_model=LedgerEntryOut)
def post_text_entry(payload: TextBillEntryRequest, authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """Direct-entry path (no voice/vision). Runs the SAME amount guardrail as
    the agent graph: over / under threshold entries are held in the approval
    queue and post to the ledger only when a human approves them."""
    merchant = scoped_merchant(payload.merchant_id, db, authorization)

    guard = validate_ledger_entry_amount(payload.taxable_value)
    if guard.requires_human_approval:
        try:
            # Build (but do not persist) the lines so the human-decided post
            # is exactly what was typed — no recomputation at decision time.
            txn = _build(payload)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        approval = ApprovalRequest(
            merchant_id=merchant.id,
            action_type="large_ledger_entry",
            payload_json=json.dumps({
                "transaction_group": txn.transaction_group,
                "lines": [
                    {
                        "account": line.account,
                        "entry_type": line.entry_type.value,
                        "amount": float(line.amount),
                        "gst_rate": line.gst_rate,
                        "memo": line.memo,
                    }
                    for line in txn.lines
                ],
            }, default=str),
            status=ApprovalStatus.PENDING,
            reason="; ".join(guard.reasons),
        )
        db.add(approval)
        db.flush()
        record_audit(
            db,
            action="approval_requested",
            object_type="approval_request",
            object_id=approval.id,
            merchant_id=merchant.id,
            actor_type="api",
            after={"status": approval.status.value, "reason": "; ".join(guard.reasons),
                   "taxable_value": payload.taxable_value},
        )
        return LedgerEntryOut(
            transaction_group="",
            guardrail_passed=guard.passed,
            guardrail_reasons=guard.reasons,
            needs_human_approval=True,
        )

    try:
        txn = _build(payload)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    for line in txn.lines:
        db.add(LedgerEntry(
            merchant_id=merchant.id,
            transaction_group=txn.transaction_group,
            account=line.account,
            entry_type=line.entry_type,
            amount=line.amount,
            gst_rate=line.gst_rate,
            source="manual",
            memo=line.memo,
        ))

    record_audit(
        db,
        action="ledger_posted",
        object_type="transaction_group",
        object_id=txn.transaction_group,
        merchant_id=merchant.id,
        actor_type="api",
        after={"source": "manual", "lines": len(txn.lines)},
    )
    return LedgerEntryOut(
        transaction_group=txn.transaction_group,
        guardrail_passed=True,
        guardrail_reasons=[],
        needs_human_approval=False,
    )


@router.post("/entries/voice")
async def post_voice_entry(
    merchant_id: str = Form(...),
    transaction_kind: str = Form("sale"),
    audio: UploadFile = File(...),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    merchant = scoped_merchant(merchant_id, db, authorization)

    audio_bytes = await audio.read()
    # Store the original note first (Cloudflare R2 free tier in prod, local
    # filesystem in dev) — re-extraction, review screens and audits all read
    # it back by key later. A storage failure refuses the entry rather than
    # accepting money data we then can't audit or re-process.
    try:
        audio_key = media.save(audio_bytes, ext="ogg", merchant_id=merchant_id)
    except media.NotConfiguredError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # UpstreamError and malformed backends
        raise HTTPException(503, f"Media storage unavailable: {e}") from e

    result = _graph.invoke({
        "merchant_id": merchant_id,
        "input_mode": "voice",
        "audio_bytes": audio_bytes,
        "transaction_kind": transaction_kind,
    })
    return _finalize_graph_result(db, merchant_id, result, media_key=audio_key)


@router.post("/entries/photo")
async def post_photo_entry(
    merchant_id: str = Form(...),
    transaction_kind: str = Form("sale"),
    gst_rate: float = Form(0.0),
    photo: UploadFile = File(...),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    merchant = scoped_merchant(merchant_id, db, authorization)

    image_bytes = await photo.read()
    try:
        image_key = media.save(image_bytes, ext="jpg", merchant_id=merchant_id)
    except media.NotConfiguredError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        raise HTTPException(503, f"Media storage unavailable: {e}") from e

    result = _graph.invoke({
        "merchant_id": merchant_id,
        "input_mode": "photo",
        "image_bytes": image_bytes,
        "transaction_kind": transaction_kind,
        "gst_rate": gst_rate,
    })
    return _finalize_graph_result(db, merchant_id, result, media_key=image_key)


def _finalize_graph_result(db: Session, merchant_id: str, result: dict, *, media_key: str | None = None):
    if result.get("needs_human_approval"):
        approval = ApprovalRequest(
            merchant_id=merchant_id,
            action_type="large_ledger_entry",
            payload_json=json.dumps({k: v for k, v in result.items() if k not in ("audio_bytes", "image_bytes")}, default=str),
            status=ApprovalStatus.PENDING,
            reason="; ".join(result.get("guardrail_reasons", [])),
        )
        db.add(approval)
        db.flush()
        record_audit(
            db,
            action="approval_requested",
            object_type="approval_request",
            object_id=approval.id,
            merchant_id=merchant_id,
            actor_type="system",
            after={"status": approval.status.value,
                   "reason": "; ".join(result.get("guardrail_reasons", [])),
                   "media_key": media_key},
        )
        return {
            "status": "needs_human_approval",
            "approval_request_id": approval.id,
            "reasons": result.get("guardrail_reasons", []),
            "media_key": media_key,
        }

    record_audit(
        db,
        action="ledger_posted",
        object_type="transaction_group",
        object_id=result.get("ledger_transaction_group"),
        merchant_id=merchant_id,
        actor_type="system",
        after={"source": result.get("input_mode"), "media_key": media_key},
    )
    return {
        "status": "posted",
        "transaction_group": result.get("ledger_transaction_group"),
        "media_key": media_key,
    }


@router.post("/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    approve: bool,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    scoped_merchant(approval.merchant_id, db, authorization)
    before_status = approval.status
    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    # A stock-pending manual entry posts only if a human actually said yes.
    if approve and approval.status == ApprovalStatus.APPROVED:
        payload = json.loads(approval.payload_json)
        if payload.get("lines"):
            for line in payload["lines"]:
                db.add(LedgerEntry(
                    merchant_id=approval.merchant_id,
                    transaction_group=payload["transaction_group"],
                    account=line["account"],
                    entry_type=EntryType(line["entry_type"]),
                    amount=line["amount"],
                    gst_rate=line["gst_rate"],
                    source="manual",
                    memo=line["memo"],
                ))
            db.flush()
            record_audit(
                db,
                action="ledger_posted",
                object_type="transaction_group",
                object_id=payload["transaction_group"],
                merchant_id=approval.merchant_id,
                actor_type="api",
                after={"source": "manual (after approval)", "lines": len(payload["lines"])},
            )
    # Human decision on a money-touching action: the single most auditable
    # event in the system. Actor is the API caller until auth lands.
    record_audit(
        db,
        action="approval_decided",
        object_type="approval_request",
        object_id=approval.id,
        merchant_id=approval.merchant_id,
        actor_type="api",
        before={"status": before_status.value},
        after={"status": approval.status.value},
    )
    return {"status": approval.status.value}


@router.get("/merchants/{merchant_id}/loan-readiness")
def check_loan_readiness(
    merchant_id: str,
    requested_amount: float,
    purpose: str,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Pulls ledger history and returns a pre-screen score. Actual OCEN
    submission is a separate, always-human-approved action
    (see integrations/ocen.py)."""
    scoped_merchant(merchant_id, db, authorization)
    db.get(Merchant, merchant_id)  # scoped_merchant already validated existence

    # In production: aggregate real LedgerEntry rows by month. Scaffold uses
    # a placeholder single-period figure so the endpoint is exercisable.
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    packet = build_cash_flow_summary(
        merchant_id=merchant_id,
        period_start=now - timedelta(days=180),
        period_end=now,
        monthly_inflows=[0.0],
        monthly_outflows=[0.0],
        gst_filed_turnover_last_12m=0.0,
        outstanding_receivables=0.0,
        outstanding_payables=0.0,
        bounce_count_last_6m=0,
        requested_amount=requested_amount,
        purpose=purpose,
    )
    return loan_readiness_score(packet)
