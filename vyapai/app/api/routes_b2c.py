from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Merchant
from app.db.session import get_db
from app.schemas import WhatsAppOrderWebhook
from app.services.order_intake import create_offline, parse_and_create

router = APIRouter(prefix="/b2c", tags=["b2c"])


@router.post("/merchants/{merchant_id}/orders")
def submit_order(merchant_id: str, payload: WhatsAppOrderWebhook, db: Session = Depends(get_db)):
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")
    if payload.offline:
        try:
            return create_offline(
                db, merchant, payload.from_number, payload.message_text,
                offer_type=payload.offer_type, offer_value=payload.offer_value,
                payment_method=payload.payment_method,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    return parse_and_create(
        db, merchant, payload.from_number, payload.message_text, reply_channel=None,
        offer_type=payload.offer_type, offer_value=payload.offer_value,
    )
