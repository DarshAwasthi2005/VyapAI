"""
WhatsApp Business Cloud API adapter (Meta).

Handles outbound messages (text, template messages for payment reminders),
inbound webhook extraction (text, voice notes and photos of order lists)
and media download, plus the webhook signature/verify-token check used by
app/api/webhooks.py.
"""
from __future__ import annotations

import hashlib
import hmac

import httpx

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

_GRAPH_BASE = "https://graph.facebook.com/v21.0"


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
    """Real Cloud API send — works as soon as WHATSAPP_TOKEN and
    WHATSAPP_PHONE_NUMBER_ID are set (Meta app + test number is free;
    production numbers need Meta business verification)."""
    _require_config()
    try:
        resp = httpx.post(
            f"{_GRAPH_BASE}/{settings.whatsapp_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_msisdn,
                "type": "text",
                "text": {"body": body},
            },
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"WhatsApp send failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach WhatsApp Graph API: {e}") from e
    return resp.json()


def extract_text(update: dict) -> tuple[str | None, str | None]:
    """Pull (text, sender_msisdn) out of a raw Meta webhook update.

    Meta's shape is deeply nested: entry[0].changes[0].value.messages[0].
    Returns (None, None) for statuses/reads/unsupported message types so the
    caller can ack-and-ignore them safely.
    """
    for entry in update.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                text = (message.get("text") or {}).get("body")
                sender = message.get("from")
                if text and sender:
                    return text, str(sender)
    return None, None


def extract_message(update: dict) -> dict | None:
    """Unwrap the first inbound message of any supported kind.

    Returns {
        "kind": "text" | "audio" | "image",
        "sender": sender msisdn,
        "text": message body (text messages only),
        "media_id": Graph media id (audio/image only),
        "media_mime": advertised MIME type (audio/image only),
    } or None for delivery/read statuses and unsupported types (sticker,
    video, ...) so the caller ack-and-ignores them.
    """
    for entry in update.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                sender = message.get("from")
                if not sender:
                    continue
                mtype = message.get("type") or ""
                sender = str(sender)
                if mtype == "text" and (message.get("text") or {}).get("body"):
                    return {"kind": "text", "sender": sender,
                            "text": message["text"]["body"]}
                # Meta labels spoken orders "audio" outside India and
                # "voice" on some clients — treat both as voice notes.
                if mtype in ("audio", "voice") and (message.get(mtype) or {}).get("id"):
                    part = message[mtype]
                    return {"kind": "audio", "sender": sender,
                            "media_id": part["id"],
                            "media_mime": part.get("mime_type", "audio/ogg")}
                if mtype == "image" and (message.get("image") or {}).get("id"):
                    part = message["image"]
                    return {"kind": "image", "sender": sender,
                            "media_id": part["id"],
                            "media_mime": part.get("mime_type", "image/jpeg")}
    return None


def download_media(media_id: str) -> tuple[bytes, str]:
    """Fetch inbound media bytes (voice notes / order-list photos) from the
    Graph API: GET /{media_id} returns a short-lived download url, then GET
    that url returns the bytes. The same WHATSAPP_TOKEN authorizes both.

    Returns (bytes, mime_type). Raises NotConfiguredError when WhatsApp
    isn't set up (nothing was downloaded yet, so a fallback ask-the-buyer
    reply is still possible), UpstreamError on Graph/API failures.
    """
    _require_config()
    return _download_media_impl(media_id, {"Authorization": f"Bearer {settings.whatsapp_token}"})


def _download_media_impl(media_id: str, headers: dict) -> tuple[bytes, str]:
    try:
        meta = httpx.get(f"{_GRAPH_BASE}/{media_id}", headers=headers, timeout=15)
        meta.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"WhatsApp media metadata failed ({e.response.status_code}) for id "
            f"{media_id}: {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach WhatsApp Graph API for media metadata: {e}") from e
    meta_json = meta.json()
    url, mime = meta_json.get("url"), meta_json.get("mime_type", "application/octet-stream")
    try:
        blob = httpx.get(url, headers=headers, timeout=30)
        blob.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"WhatsApp media download failed ({e.response.status_code}) for id {media_id}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not download WhatsApp media: {e}") from e
    return blob.content, mime


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
