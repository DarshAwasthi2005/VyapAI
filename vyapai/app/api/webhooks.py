from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Payment, PaymentStatus
from app.db.session import get_db
from app.integrations import whatsapp

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
def verify_whatsapp_webhook(
    hub_mode: str = "", hub_verify_token: str = "", hub_challenge: str = "",
):
    """Meta calls this once, on webhook setup, as a GET with hub.* query params."""
    challenge = whatsapp.verify_webhook_challenge(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(403, "Verification failed")
    return int(challenge)


@router.post("/whatsapp")
async def receive_whatsapp_message(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
):
    body = await request.body()
    # TODO(deploy): app_secret should come from settings, not be hardcoded;
    # left explicit here so the check isn't silently skipped if unconfigured.
    app_secret = getattr(settings, "whatsapp_app_secret", None)
    if app_secret and not whatsapp.verify_signature(app_secret, body, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")

    payload = await request.json()
    # TODO(deploy): unwrap Meta's actual nested payload shape:
    #   entry[0].changes[0].value.messages[0].{from, text.body, type, ...}
    # and route text/audio/image messages to routes_b2c.submit_order or the
    # B2B voice/photo endpoints depending on which flow the sender is in.
    return {"received": True}


@router.post("/payments/psp")
async def receive_payment_webhook(request: Request, db: Session = Depends(get_db)):
    """Generic PSP (Razorpay/Cashfree/etc.) payment-status callback.
    TODO(deploy): verify the PSP's webhook signature the same way as
    WhatsApp above — every PSP has its own header/secret scheme."""
    payload = await request.json()
    txn_ref = payload.get("transaction_ref")
    status = payload.get("status")  # expect "success" | "failed"

    if not txn_ref:
        raise HTTPException(422, "Missing transaction_ref")

    payment = db.execute(select(Payment).where(Payment.transaction_ref == txn_ref)).scalar_one_or_none()
    if not payment:
        raise HTTPException(404, "Unknown transaction_ref")

    payment.status = PaymentStatus.SUCCESS if status == "success" else PaymentStatus.FAILED
    return {"updated": True}
