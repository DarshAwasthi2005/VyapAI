from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.agents.graph_b2b import build_b2b_graph
from app.db.models import ApprovalRequest, ApprovalStatus, EntryType, LedgerEntry, Merchant
from app.db.session import get_db
from app.schemas import LedgerEntryOut, MerchantCreate, MerchantOut, TextBillEntryRequest
from app.services.accounting import build_purchase_transaction, build_sale_transaction
from app.services.credit import build_cash_flow_summary, loan_readiness_score

router = APIRouter(prefix="/b2b", tags=["b2b"])
_graph = build_b2b_graph()


@router.post("/merchants", response_model=MerchantOut)
def create_merchant(payload: MerchantCreate, db: Session = Depends(get_db)):
    merchant = Merchant(**payload.model_dump())
    db.add(merchant)
    db.flush()
    return merchant


@router.post("/entries/text", response_model=LedgerEntryOut)
def post_text_entry(payload: TextBillEntryRequest, db: Session = Depends(get_db)):
    """Direct-entry path (no voice/vision) — still runs through the same
    guardrails as the agent graph, just skipping the ingest node."""
    merchant = db.get(Merchant, payload.merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")

    builder = build_sale_transaction if payload.transaction_kind == "sale" else build_purchase_transaction
    try:
        txn = builder(
            taxable_value=payload.taxable_value,
            gst_rate=payload.gst_rate,
            party=payload.party_name,
            source="manual",
        )
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
    db: Session = Depends(get_db),
):
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")

    audio_bytes = await audio.read()
    result = _graph.invoke({
        "merchant_id": merchant_id,
        "input_mode": "voice",
        "audio_bytes": audio_bytes,
        "transaction_kind": transaction_kind,
    })
    return _finalize_graph_result(db, merchant_id, result)


@router.post("/entries/photo")
async def post_photo_entry(
    merchant_id: str = Form(...),
    transaction_kind: str = Form("sale"),
    gst_rate: float = Form(0.0),
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")

    image_bytes = await photo.read()
    result = _graph.invoke({
        "merchant_id": merchant_id,
        "input_mode": "photo",
        "image_bytes": image_bytes,
        "transaction_kind": transaction_kind,
        "gst_rate": gst_rate,
    })
    return _finalize_graph_result(db, merchant_id, result)


def _finalize_graph_result(db: Session, merchant_id: str, result: dict):
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
        return {
            "status": "needs_human_approval",
            "approval_request_id": approval.id,
            "reasons": result.get("guardrail_reasons", []),
        }

    return {
        "status": "posted",
        "transaction_group": result.get("ledger_transaction_group"),
    }


@router.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, approve: bool, db: Session = Depends(get_db)):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(404, "Approval request not found")
    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    return {"status": approval.status.value}


@router.get("/merchants/{merchant_id}/loan-readiness")
def check_loan_readiness(merchant_id: str, requested_amount: float, purpose: str, db: Session = Depends(get_db)):
    """Pulls ledger history and returns a pre-screen score. Actual OCEN
    submission is a separate, always-human-approved action
    (see integrations/ocen.py)."""
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")

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
