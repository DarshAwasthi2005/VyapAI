"""
WhatsApp Business Cloud API adapter (Meta).

Handles outbound messages (text, template messages for payment reminders)
and provides the webhook signature/verify-token check used by
app/api/webhooks.py for inbound messages.
"""
from __future__ import annotations

import hashlib
import hmac

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError


def verify_webhook_challenge(mode: str, token: str, challenge: str) -> str | None:
    """Meta's webhook verification handshake (GET request on setup)."""
    if not settings.whatsapp_verify_token:
        raise NotConfiguredError("WhatsApp webhook", ["WHATSAPP_VERIFY_TOKEN"])
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return challenge
    return None


def verify_signature(app_secret: str, payload_body: bytes, signature_header: str) -> bool:
    """Validate X-Hub-Signature-256 on inbound webhook POSTs."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def send_text_message(to_msisdn: str, body: str) -> dict:
    _require_config()
    # TODO(deploy): real call, e.g.
    #   resp = httpx.post(
    #       f"https://graph.facebook.com/v20.0/{settings.whatsapp_phone_number_id}/messages",
    #       headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
    #       json={"messaging_product": "whatsapp", "to": to_msisdn,
    #             "type": "text", "text": {"body": body}},
    #       timeout=10,
    #   )
    #   resp.raise_for_status()
    #   return resp.json()
    raise UpstreamError("WhatsApp send is stubbed. Wire it up in integrations/whatsapp.py.")


def send_payment_reminder(to_msisdn: str, amount_inr: float, upi_link: str, escalation_level: int) -> dict:
    """escalation_level: 0=gentle, 1=firm, 2=final notice. Tone, never threats."""
    templates = {
        0: f"Namaste! A gentle reminder — ₹{amount_inr:,.2f} is pending on your last order. "
           f"Pay anytime here: {upi_link}",
        1: f"Reminder: ₹{amount_inr:,.2f} has been pending for a few days now. "
           f"Please settle when you get a chance: {upi_link}",
        2: f"Final reminder: ₹{amount_inr:,.2f} is still outstanding. Please clear this at the "
           f"earliest to keep your account in good standing: {upi_link}",
    }
    body = templates.get(escalation_level, templates[0])
    return send_text_message(to_msisdn, body)


def _require_config() -> None:
    if not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
        raise NotConfiguredError(
            "WhatsApp", ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]
        )
