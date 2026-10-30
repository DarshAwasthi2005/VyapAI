"""Messaging channels: Telegram (free, primary) and WhatsApp Cloud API.

One place that knows how to *send* on either channel, which channel a shop
prefers per role, and what to tell the dashboard about setup state. Adapters
remain dumb transports; policy lives here.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import Merchant, MerchantChannelPreference
from app.integrations import telegram, whatsapp


class ChannelOutcome:
    def __init__(self, delivered: bool, error: str | None = None):
        self.delivered = delivered
        self.error = error


def send(channel: str, chat_id: str, body: str,
         reply_markup: dict | None = None) -> ChannelOutcome:
    """Dispatch a text, optionally with an inline keyboard (Telegram only —
    WhatsApp has no equivalent and ignores it). Never raises: messaging
    failures are reported, not fatal — an order that confirmed fine must
    not 500 because a bot is idle."""
    try:
        if channel == "telegram":
            telegram.send_text_message(chat_id, body, reply_markup=reply_markup)
        elif channel == "whatsapp":
            whatsapp.send_text_message(chat_id, body)
        else:
            return ChannelOutcome(False, f"unknown channel: {channel}")
        return ChannelOutcome(True)
    except Exception as e:  # NotConfiguredError / UpstreamError
        return ChannelOutcome(False, str(e))


def is_configured(channel: str) -> bool:
    """Whether OUTBOUND sends on this channel can work right now (env only)."""
    if channel == "telegram":
        return bool(telegram.settings.telegram_bot_token)
    if channel == "whatsapp":
        return bool(whatsapp.settings.whatsapp_token and whatsapp.settings.whatsapp_phone_number_id)
    return False


def is_linked(merchant: Merchant, channel: str) -> bool:
    """Whether a chat is associated with this SHOP on this channel."""
    if channel == "telegram":
        return bool(merchant.telegram_chat_id)
    if channel == "whatsapp":
        return bool(merchant.whatsapp_chat_id)
    return False


def resolve_role_channel(db, merchant: Merchant, role: str) -> str | None:
    """The channel a shop uses for a role: explicit preference first, then
    sensible defaults among the channels actually set up (any env config
    plus a linked chat). Roles: \"otp\", \"orders\"."""
    row = db.execute(
        select(MerchantChannelPreference).where(
            MerchantChannelPreference.merchant_id == merchant.id,
            MerchantChannelPreference.role == role,
        )
    ).scalar_one_or_none()
    candidates = [row.channel] if row else ["telegram", "whatsapp"]
    for c in candidates:
        if is_configured(c) and is_linked(merchant, c):
            return c
    return None


def set_role_channel(db, merchant: Merchant, role: str, channel: str) -> None:
    if role not in ("otp", "orders"):
        raise ValueError(f"unknown channel role: {role}")
    if channel not in ("telegram", "whatsapp"):
        raise ValueError(f"unknown channel: {channel}")
    row = db.execute(
        select(MerchantChannelPreference).where(
            MerchantChannelPreference.merchant_id == merchant.id,
            MerchantChannelPreference.role == role,
        )
    ).scalar_one_or_none()
    if row:
        row.channel = channel
    else:
        db.add(MerchantChannelPreference(merchant_id=merchant.id, role=role, channel=channel))
    db.flush()


def status_for(merchant: Merchant) -> dict:
    """What the dashboard Channels card needs. Concretely actionable: each
    channel reports env-configured (outbound possible) and linked (a chat is
    associated with this shop)."""
    def _status(channel: str) -> dict:
        configured = is_configured(channel)
        linked = is_linked(merchant, channel)
        if channel == "telegram":
            setup = "TELEGRAM_BOT_TOKEN (free from @BotFather)"
            # When the bot exists, show its real @username so the operator
            # knows exactly where to send /start <code>.
            bot_username = telegram.get_bot_username() if configured else None
        else:
            setup = "WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID (Meta Cloud API)"
            bot_username = None
        return {
            "configured": configured,
            "linked": linked,
            "ready": configured and linked,
            "setup_hint": None if configured else setup,
            "bot_username": bot_username,
        }

    return {"telegram": _status("telegram"), "whatsapp": _status("whatsapp")}
