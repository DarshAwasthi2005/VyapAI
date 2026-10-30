"""Per-shop messaging-channel management.

GET  /b2b/channels        - setup status for Telegram + WhatsApp, current prefs
POST /b2b/channels/pref   - choose which channel a role uses
POST /b2b/channels/wa-verify - simulates an inbound "link" message to pair a
                               WhatsApp chat locally (real deployments link
                               on first contact via the Meta webhook)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes_auth import scoped_merchant
from app.db.session import get_db
from app.services import channels as channelsvc

router = APIRouter(prefix="/b2b/channels", tags=["channels"])


class ChannelPrefRequest(BaseModel):
    role: str = Field(pattern="^(otp|orders)$")
    channel: str = Field(pattern="^(telegram|whatsapp)$")


@router.get("")
def channels_status(merchant_id: str, db: Session = Depends(get_db),
                    authorization: str = Header(default="")):
    """Setup state + per-role preference for this shop. The merchant must be
    signed in; channels belong to the shop, not to the browser."""
    merchant = scoped_merchant(merchant_id, db, authorization)
    return channelsvc.status_for(merchant)


@router.post("/pref")
def set_pref(merchant_id: str, payload: ChannelPrefRequest, db: Session = Depends(get_db),
             authorization: str = Header(default="")):
    merchant = scoped_merchant(merchant_id, db, authorization)
    try:
        channelsvc.set_role_channel(db, merchant, payload.role, payload.channel)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.commit()
    return {"status": "ok", "role": payload.role, "channel": payload.channel}
