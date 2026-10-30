"""
Telegram Bot adapter — the zero-cost messaging channel.

Why Telegram is the pilot channel: a bot token is free forever from
@BotFather, there is no per-message fee and no template regime, so order
intake, confirmations, khata balances and payment reminders all run at ₹0
indefinitely. WhatsApp (integrations/whatsapp.py) is the later, metered
channel — this module deliberately mirrors its interface
(send_text_message / send_payment_reminder / inbound verification) so the
rest of the code can treat messaging as pluggable and switch channels
with configuration alone.

Telegram's Bot API is a plain HTTPS POST, so unlike the WhatsApp stub this
adapter is fully wired: real sends work the moment TELEGRAM_BOT_TOKEN is
set. Inbound updates are verified with the X-Telegram-Bot-Api-Secret-Token
header that you pass to setWebhook (see api/webhooks.py).
"""
from __future__ import annotations

import hmac

import httpx

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

_BASE = "https://api.telegram.org"

# MIME per file extension on the paths getFile hands back (voice notes land
# as .ogg, photos as .jpg). Telegram's file objects carry no mime_type, so
# the extension of the returned file_path is the authoritative hint.
_MIME_BY_EXT = {
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp",
}


def _api_url(method: str) -> str:
    if not settings.telegram_bot_token:
        raise NotConfiguredError(
            "Telegram Bot", ["TELEGRAM_BOT_TOKEN (free from @BotFather)"]
        )
    return f"{_BASE}/bot{settings.telegram_bot_token}/{method}"


def verify_secret_token(header_value: str) -> bool:
    """Constant-time check of the init_vector Telegram echoes back on every
    webhook delivery. Returns False when TELEGRAM_SECRET_TOKEN is unset, so
    an unconfigured secret never silently accepts traffic."""
    if not settings.telegram_secret_token:
        return False
    return hmac.compare_digest(header_value or "", settings.telegram_secret_token)


def send_text_message(chat_id: str | int, body: str,
                      reply_markup: dict | None = None) -> dict:
    """Send a plain text message. Real call — works as soon as the token is
    set (no Meta app review queue, unlike WhatsApp). reply_markup attaches
    an inline keyboard, e.g. {"inline_keyboard": [[{...}, {...}]]} for the
    payment-confirmation buttons; omitted entirely for plain replies."""
    payload: dict = {"chat_id": chat_id, "text": body}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(
            _api_url("sendMessage"),
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Telegram send failed ({e.response.status_code}): {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Telegram API: {e}") from e
    data = resp.json()
    if not data.get("ok"):
        raise UpstreamError(f"Telegram API rejected sendMessage: {data}")
    return data


def send_payment_reminder(chat_id: str | int, amount_inr: float, upi_link: str, escalation_level: int) -> dict:
    """Same roles as the WhatsApp reminder: 0=gentle, 1=firm, 2=final notice.
    Tone, never threats — and on Telegram every one of these is free."""
    templates = {
        0: f"Namaste! A gentle reminder — ₹{amount_inr:,.2f} is pending on your last order. "
           f"Pay anytime here: {upi_link}",
        1: f"Reminder: ₹{amount_inr:,.2f} has been pending for a few days now. "
           f"Please settle when you get a chance: {upi_link}",
        2: f"Final reminder: ₹{amount_inr:,.2f} is still outstanding. Please clear this at the "
           f"earliest to keep your account in good standing: {upi_link}",
    }
    body = templates.get(escalation_level, templates[0])
    return send_text_message(chat_id, body)


def get_bot_username() -> str | None:
    """Fetch the bot's @username via getMe so the dashboard can show the
    merchant exactly which bot to send /start <code> to. None when the API
    call fails (never blocks linking)."""
    try:
        resp = httpx.get(_api_url("getMe"), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data["result"].get("username")
    except httpx.HTTPError:
        pass
    return None


def fetch_recent_updates(offset: int = 0, timeout_seconds: int = 0) -> list[dict]:
    """Outbound long-poll: pull recent updates via getUpdates. This is the
    localhost alternative to setWebhook — no public URL is needed; the app
    initiates the connection to Telegram. Returns raw update dicts (oldest
    first). offset acknowledges all updates before it.

    An unconfigured bot token raises NotConfiguredError at call time.
    If a webhook was ever set (conflict error), Telegram refuses getUpdates
    and we surface that as an UpstreamError telling the operator which way
    this bot is wired."""
    try:
        resp = httpx.post(
            _api_url("getUpdates"),
            json={"offset": offset, "timeout": timeout_seconds,
                  "allowed_updates": ["message", "callback_query"]},
            timeout=max(15, timeout_seconds + 5),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:200]
        if "webhook" in detail or e.response.status_code == 409:
            raise UpstreamError(
                "This bot has a webhook set, so getUpdates is refused. "
                "Either delete the webhook (deleteWebhook) or use webhook delivery."
            ) from e
        raise UpstreamError(f"Telegram getUpdates failed ({e.response.status_code}): {detail}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Telegram API: {e}") from e
    data = resp.json()
    if not data.get("ok"):
        raise UpstreamError(f"Telegram API rejected getUpdates: {data}")
    return data.get("result", [])


def extract_callback(update: dict) -> dict | None:
    """Unwrap an inline-button tap (a callback_query update).

    Returns {callback_id, chat_id, message_id, text, data} or None when the
    update isn't a usable tap. The tapped message travels with the callback,
    so the handler can edit the very message the button was on.
    """
    cb = update.get("callback_query") or {}
    if not cb.get("id") or not cb.get("data"):
        return None
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    return {
        "callback_id": str(cb["id"]),
        "chat_id": str(chat_id),
        "message_id": message.get("message_id"),
        "text": message.get("text") or "",
        "data": str(cb["data"]),
    }


def answer_callback_query(callback_query_id: str, text: str = "",
                          show_alert: bool = False) -> dict:
    """Clear the tap's loading spinner on Telegram's side (optionally with
    an alert dialog). Raises NotConfiguredError/UpstreamError like every
    other Bot API call — callers treat it as best-effort."""
    try:
        resp = httpx.post(
            _api_url("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id,
                  "text": text, "show_alert": show_alert},
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"Telegram answerCallbackQuery failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Telegram API: {e}") from e
    data = resp.json()
    if not data.get("ok"):
        raise UpstreamError(f"Telegram API rejected answerCallbackQuery: {data}")
    return data


def edit_message_text(chat_id: str | int, message_id: int, text: str) -> dict:
    """Edit a message this bot sent — used to stamp a tapped payment button
    with its result. An empty inline_keyboard REMOVES the buttons, so a tap
    can't be repeated from the chat."""
    try:
        resp = httpx.post(
            _api_url("editMessageText"),
            json={"chat_id": chat_id, "message_id": message_id, "text": text,
                  "reply_markup": {"inline_keyboard": []}},
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"Telegram editMessageText failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Telegram API: {e}") from e
    data = resp.json()
    if not data.get("ok"):
        raise UpstreamError(f"Telegram API rejected editMessageText: {data}")
    return data


def extract_media(update: dict) -> dict | None:
    """Unwrap the first inbound message of any supported kind — the same
    shape whatsapp.extract_message returns, so the shared webhook handlers
    treat both channels identically.

    Returns {
        "kind": "text" | "audio" | "image",
        "chat_id": sender chat id as a string,
        "text": message body (text messages only),
        "media_id": file_id (audio/image only),
        "media_mime": advertised MIME type (audio/image only),
    } or None for edits, callbacks, stickers, video and every other
    unsupported type, so the caller can ack-and-ignore them safely.
    """
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    chat = str(chat_id)

    text = message.get("text")
    if text:
        return {"kind": "text", "chat_id": chat, "text": text}

    # Voice notes ("voice") and uploaded audio files ("audio") are both
    # spoken orders — Meta labels them the same way on WhatsApp.
    for key in ("voice", "audio"):
        part = message.get(key) or {}
        if part.get("file_id"):
            return {"kind": "audio", "chat_id": chat,
                    "media_id": part["file_id"],
                    "media_mime": part.get("mime_type", "audio/ogg")}

    # Photos arrive as an array of sizes — the LAST entry is the largest,
    # and Telegram photos are always JPEG.
    sizes = message.get("photo")
    if isinstance(sizes, list) and sizes:
        largest = sizes[-1]
        if isinstance(largest, dict) and largest.get("file_id"):
            return {"kind": "image", "chat_id": chat,
                    "media_id": largest["file_id"],
                    "media_mime": "image/jpeg"}
    return None


def extract_text(update: dict) -> tuple[str | None, str | None]:
    """Pull (text, sender_chat_id) out of a raw webhook update dict.

    Returns (None, None) for updates carrying no message text (edits,
    callbacks, voice notes, photos) so the caller can ack-and-ignore them
    safely. Thin wrapper over extract_media for text-only callers.
    """
    msg = extract_media(update)
    if msg is None or msg["kind"] != "text":
        return None, None
    return msg["text"], msg["chat_id"]


def download_media(file_id: str) -> tuple[bytes, str]:
    """Fetch inbound media bytes (voice notes / order-list photos): getFile
    resolves the internal file path, then GET that path returns the bytes —
    the same two-step shape as the WhatsApp Graph download, so the shared
    webhook handlers call either adapter interchangeably.

    Returns (bytes, mime_type). Raises NotConfiguredError when no bot token
    is set (nothing was downloaded yet, so a fallback ask-the-buyer reply is
    still possible), UpstreamError on Bot API failures.
    """
    if not settings.telegram_bot_token:
        raise NotConfiguredError(
            "Telegram Bot", ["TELEGRAM_BOT_TOKEN (free from @BotFather)"]
        )
    try:
        resp = httpx.post(_api_url("getFile"), json={"file_id": file_id}, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"Telegram getFile failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Telegram API: {e}") from e
    try:
        data = resp.json()
    except ValueError as e:
        raise UpstreamError(f"Telegram getFile returned a non-JSON body: {e}") from e
    if not data.get("ok"):
        raise UpstreamError(f"Telegram API rejected getFile: {data}")
    file_path = (data.get("result") or {}).get("file_path")
    if not file_path:
        raise UpstreamError(f"Telegram getFile returned no file_path: {data}")

    try:
        blob = httpx.get(f"{_BASE}/file/bot{settings.telegram_bot_token}/{file_path}", timeout=30)
        blob.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(
            f"Telegram file download failed ({e.response.status_code}) for {file_path}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not download Telegram file: {e}") from e

    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    return blob.content, _MIME_BY_EXT.get(ext, "application/octet-stream")
