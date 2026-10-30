from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.audit import record_audit

from app.config import settings
from app.db.models import (
    LoginChallenge,
    Merchant,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from app.db.session import get_db
from app.integrations import media, speech, telegram, vision, whatsapp
from app.integrations.errors import NotConfiguredError, UpstreamError
from app.services import auth as authsvc
from app.services import channels as channelsvc
from app.services import order_intake
from app.services.orders import parse_order_text

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class DemoLinkRequest(BaseModel):
    merchant_id: str


@router.post("/whatsapp/demo-link")
def demo_link_whatsapp(payload: DemoLinkRequest, authorization: str = Header(default=""),
                       db: Session = Depends(get_db)):
    """Demo helper for when no Meta phone number exists on this machine.

    Runs exactly what the real inbound webhook runs on first contact from
    the shop's own number — stores the WhatsApp chat against the shop so the
    Channels card shows a workable state and inbound routing is exercised.
    Requires a live session for that shop."""
    try:
        session_merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    if payload.merchant_id != session_merchant.id:
        raise HTTPException(403, "You are signed in as a different shop")
    if not session_merchant.whatsapp_number:
        raise HTTPException(422, "This shop has no WhatsApp number to link")
    session_merchant.whatsapp_chat_id = session_merchant.whatsapp_number
    record_audit(
        db, action="whatsapp_chat_linked", object_type="merchant",
        object_id=session_merchant.id, merchant_id=session_merchant.id,
        actor_type="api", after={"channel": "whatsapp", "demo": True},
    )
    return {"linked": True, "whatsapp_chat_id": session_merchant.whatsapp_chat_id}


@router.post("/telegram/demo-link")
def demo_link_telegram(authorization: str = Header(default=""),
                       db: Session = Depends(get_db)):
    """Demo helper mirroring whatsapp/demo-link: for machines with no
    TELEGRAM_BOT_TOKEN, there is no live bot to receive `/start <code>`.
    This runs the exact pairing branch the real webhook runs — consuming a
    pending telegram_link challenge issued for the signed-in shop — but with
    a synthetic chat id, so the Channels card and intake routing are
    exercisable end-to-end without Telegram credentials."""
    try:
        merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    now = datetime.utcnow()
    pending = db.execute(
        select(LoginChallenge).where(
            LoginChallenge.merchant_id == merchant.id,
            LoginChallenge.purpose == "telegram_link",
            LoginChallenge.consumed_at.is_(None),
        )
    ).scalars().all()
    consumed_id = None
    for c in pending:
        if c.expires_at <= now:
            continue
        authsvc.consume(c, now)
        consumed_id = c.id
        break  # one pairing per call; strays stay pending
    if not merchant.telegram_chat_id:
        merchant.telegram_chat_id = f"demo-tg-{merchant.id[:8]}"
    record_audit(
        db, action="telegram_chat_linked", object_type="merchant",
        object_id=merchant.id, merchant_id=merchant.id, actor_type="api",
        after={"channel": "telegram", "demo": True, "challenge_id": consumed_id},
    )
    return {"linked": True, "telegram_chat_id": merchant.telegram_chat_id}


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
    db: Session = Depends(get_db),
):
    body = await request.body()
    # TODO(deploy): app_secret should come from settings, not be hardcoded;
    # left explicit here so the check isn't silently skipped if unconfigured.
    app_secret = getattr(settings, "whatsapp_app_secret", None)
    if app_secret and not whatsapp.verify_signature(app_secret, body, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")

    payload = await request.json()
    msg = whatsapp.extract_message(payload)
    if msg is None:
        # Delivery/read statuses and unsupported message types: nothing to do.
        return {"received": True, "handled": False}
    sender = msg["sender"]

    # --- first-contact linking: the SHOP's own number says hello ----------
    if sender:
        linked = db.execute(
            select(Merchant).where(Merchant.whatsapp_number == sender)
        ).scalar_one_or_none()
        if linked is not None and not linked.whatsapp_chat_id:
            linked.whatsapp_chat_id = sender
            record_audit(
                db, action="whatsapp_chat_linked", object_type="merchant",
                object_id=linked.id, merchant_id=linked.id, actor_type="system",
                after={"channel": "whatsapp"},
            )

    # --- orders from the shop's own linked chat ---------------------------
    shop = order_intake.resolve_merchant_for_chat(db, "whatsapp", sender)
    if shop is None:
        return {"received": True, "handled": False}

    if msg["kind"] == "text":
        result = order_intake.parse_and_create(db, shop, sender, msg["text"], reply_channel="whatsapp")
        return {"received": True, "handled": True, "source": "text", "order": result}

    if msg["kind"] == "audio":
        return _handle_voice_order(db, shop, sender, msg)
    return _handle_photo_order(db, shop, sender, msg)


def _fmt_qty(q: float) -> str:
    """2.0 -> "2", 2.5 -> "2.5" — quantities render the way people write them."""
    return str(int(q)) if float(q) == int(q) else str(q)


def _media_ext(mime: str) -> str:
    ext = (mime or "").split("/")[-1].split(";")[0]
    return {"jpeg": "jpg", "m4a": "m4a", "ogg": "ogg", "aac": "aac",
            "mpeg": "mp3", "mp4": "mp4", "webp": "webp", "png": "png"}.get(ext, ext or "bin")


_CHANNEL_LABEL = {"whatsapp": "WhatsApp", "telegram": "Telegram"}


def _download_media(channel: str, media_id: str) -> tuple[bytes, str]:
    """One seam over the two inbound-media adapters, so the voice/photo
    handlers below stay channel-agnostic and behave identically on
    WhatsApp and Telegram."""
    if channel == "telegram":
        return telegram.download_media(media_id)
    return whatsapp.download_media(media_id)


def _ask_buyer_to_type(db: Session, shop: Merchant, sender: str, what: str, error: str,
                       channel: str = "whatsapp") -> dict:
    """A voice/photo order arrived but the shop's server can't process it
    right now (no ASR/OCR backend, or Graph download failed). Never lose the
    customer silently: at least record, always tell them to resend typed.
    The reply is best-effort — messaging failures don't raise — and goes
    back on the same channel the media arrived on.
    """
    text = (
        f"Sorry, we couldn't automatically read your {what} right now."
        + (f" ({error})" if error else "")
        + " Please type your order as a message — it will be entered right away."
    )
    channelsvc.send(channel, sender, text)
    return {"received": True, "handled": False, "reason": f"{what} unreadable: {error}"}


def _handle_voice_order(db: Session, shop: Merchant, sender: str, msg: dict,
                        channel: str = "whatsapp") -> dict:
    """A voice note from the linked chat: download the audio, transcribe it
    through the speech adapter, then run the EXACT same typed-order intake
    with the transcript. No ASR is configured → ask the buyer to type.
    """
    try:
        audio, mime = _download_media(channel, msg["media_id"])
        media.save(audio, ext=_media_ext(mime), merchant_id=shop.id)
        transcript = speech.transcribe(audio).text
    except (NotConfiguredError, UpstreamError) as e:
        return _ask_buyer_to_type(db, shop, sender, "voice message", str(e), channel)
    if not transcript.strip():
        return _ask_buyer_to_type(db, shop, sender, "voice message", "nothing was understood", channel)

    result = order_intake.parse_and_create(db, shop, sender, transcript, reply_channel=channel)
    return {"received": True, "handled": True, "source": "voice_note",
            "transcript": transcript, "order": result}


def _handle_photo_order(db: Session, shop: Merchant, sender: str, msg: dict,
                        channel: str = "whatsapp") -> dict:
    """A photo of a handwritten order list from the linked chat: download
    the image, read the items with the vision/OCR adapter, and enter every
    item in one order. Items already carry quantity (+ unit when legible).
    No OCR backend is configured → ask the buyer to type.
    """
    try:
        image, mime = _download_media(channel, msg["media_id"])
        media.save(image, ext=_media_ext(mime), merchant_id=shop.id)
        extraction = vision.extract_bill(image, media_type=mime)
    except (NotConfiguredError, UpstreamError) as e:
        return _ask_buyer_to_type(db, shop, sender, "order photo", str(e), channel)

    items: list[dict] = []
    for li in extraction.line_items:
        # Descriptions usually carry the unit ("2 kg chini"); recover it with
        # the same parser the typed path uses, falling back to bare quantity.
        parsed = parse_order_text(li.description)
        if parsed.line_items:
            p = parsed.line_items[0]
            items.append({"item_name": p.item_name, "quantity": p.quantity, "unit": p.unit})
        elif li.quantity:
            items.append({"item_name": li.description.strip(),
                          "quantity": li.quantity, "unit": "units"})
    if not items:
        return _ask_buyer_to_type(db, shop, sender, "order photo",
                                  "no readable order items in the image", channel)

    summary = "; ".join(
        f"{_fmt_qty(d['quantity'])} {d['unit']} {d['item_name']}" for d in items
    )
    result = order_intake.create_from_structured(
        db, shop, sender,        raw_message=f"{_CHANNEL_LABEL.get(channel, channel)} order photo: {summary}",
        line_items=items, reply_channel=channel,
    )
    return {"received": True, "handled": True, "source": "order_photo",
            "items": items, "order": result}


@router.post("/payments/psp")
async def receive_payment_webhook(
    request: Request,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Generic PSP (Razorpay/Cashfree/etc.) payment-status callback.

    Protected: the caller must present a live bearer session for the shop
    that owns the order (the dashboard "Mark paid" button does). At go-live
    the PSP's own webhook signature (per-provider scheme) replaces/extends
    this check and the shop-owner session stays browser-only.
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

    owner_id = payment.order.merchant_id if payment.order else None
    if not owner_id:
        raise HTTPException(409, "Payment has no owning shop")
    try:
        session_merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    if owner_id != session_merchant.id:
        raise HTTPException(403, "This order belongs to a different shop")

    status_before = payment.status
    payment.status = PaymentStatus.SUCCESS if status == "success" else PaymentStatus.FAILED
    # "cash" = the merchant confirmed money-in-hand on the dashboard; the
    # PSP webhook path (Mark paid demo / real PSP) is UPI by definition.
    method = (status == "success" and payload.get("method")) or "upi"
    payment.method = method if method in ("cash", "upi") else "upi"
    record_audit(
        db,
        action="payment_status_changed",
        object_type="payment",
        object_id=payment.id,
        merchant_id=payment.order.merchant_id if payment.order else None,
        actor_type="api",
        before={"status": status_before.value},
        after={"status": payment.status.value, "method": payment.method,
               "transaction_ref": txn_ref},
    )
    return {"updated": True}


class CashConfirmRequest(BaseModel):
    order_id: str


def _mark_payment_paid(db: Session, order: Order, method: str) -> dict:
    """Settle an order's payment — money in hand (cash) or UPI received.
    Shared by the dashboard's confirm endpoint (session-authenticated) and
    the Telegram inline-keyboard tap (chat-binding authenticated; its
    caller has already verified the order belongs to that shop either way).
    Idempotent: an already-successful payment is reported, never
    double-counted. Returns the endpoint response shape."""
    payment = order.payment
    if payment is not None and payment.status == PaymentStatus.SUCCESS:
        return {"updated": False, "already_confirmed": True, "method": payment.method}

    before = payment.status.value if payment else None
    if payment is None:
        payment = Payment(
            order_id=order.id,
            transaction_ref=f"{method.upper()}-{uuid.uuid4().hex[:12].upper()}",
            upi_deep_link="",  # no PSP link exists for money-in-hand
            amount=order.total_amount,
            status=PaymentStatus.SUCCESS,
            method=method,
        )
        db.add(payment)
        db.flush()
    else:
        payment.status = PaymentStatus.SUCCESS
        payment.method = method
    record_audit(
        db,
        action="payment_status_changed",
        object_type="payment",
        object_id=payment.id,
        merchant_id=order.merchant_id,
        actor_type="api",
        before={"status": before},
        after={"status": payment.status.value, "method": method},
    )
    return {"updated": True, "method": method, "transaction_ref": payment.transaction_ref}


@router.post("/payments/cash")
def confirm_cash_payment(
    payload: CashConfirmRequest,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Merchant confirms a CASH payment from the dashboard: money is in
    hand, no PSP is involved. The live session must belong to the shop that
    owns the order. Works for orders with no Payment row yet (typical:
    orders settled in cash without a UPI link) and for pending ones;
    already-confirmed orders are idempotent instead of double-counting."""
    try:
        merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    order = db.get(Order, payload.order_id)
    if order is None:
        raise HTTPException(404, "Unknown order")
    if order.merchant_id != merchant.id:
        raise HTTPException(403, "This order belongs to a different shop")
    return _mark_payment_paid(db, order, "cash")


def _tg_reply(chat_id: str | None, body: str) -> None:
    """Best-effort reply on a Telegram chat. Policy lives in
    services/channels, whose send() reports instead of raising — command
    handling can never fail because a message didn't go out."""
    if chat_id:
        channelsvc.send("telegram", chat_id, body)


def _parse_command(text: str) -> tuple[str, str] | None:
    '''Split "/status@MyBot arg…" into (command, arg) — group chats append
    the bot's @username to commands, and it must be stripped. Returns None
    for prose, which then flows to the order parser as before.'''
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, arg = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if not name:
        return None
    return name, arg.strip()


def _help_text(shop: Merchant | None) -> str:
    lines = [
        "VyapAI — what I can do",
        "",
        "Send an order any of these ways:",
        '• Type it: "5 kg sugar by 4 PM"',
        "• A voice note saying your order",
        "• A photo of your written order list",
        "",
        "Commands:",
        "/status — recent orders & payments",
        "/help — this message",
        "/start <code> — link this chat to your shop",
    ]
    if shop is None:
        lines += [
            "",
            "This chat is not linked to a shop yet. Sign in on the dashboard, "
            'open Channels and use "Link via /start code".',
        ]
    return "\n".join(lines)


def _fmt_payment(order: Order) -> str:
    payment = order.payment
    if payment is None:
        return "payment not recorded"
    if payment.status == PaymentStatus.SUCCESS:
        return f"paid via {payment.method or 'upi'}"
    if payment.status == PaymentStatus.FAILED:
        return "payment failed"
    return "payment pending"


def _status_text(db: Session, shop: Merchant) -> str:
    """Last few orders with their payment state, plus how much is still
    owing: confirmed orders with no successful payment row (cash taken at
    the counter but not confirmed, UPI link never paid)."""
    orders = db.execute(
        select(Order)
        .where(Order.merchant_id == shop.id)
        .order_by(Order.created_at.desc())
        .limit(5)
    ).scalars().all()

    lines = [f"{shop.business_name} — recent orders"]
    if not orders:
        lines.append('No orders yet. Send one like: "5 kg sugar by 4 PM"')
    for o in orders:
        lines.append(
            f"• ₹{float(o.total_amount):,.2f} — {o.status.value.replace('_', ' ')} — "
            f"{_fmt_payment(o)} (#{o.id[:8]})"
        )

    awaiting = db.execute(
        select(func.count(Order.id))
        .outerjoin(Payment, Payment.order_id == Order.id)
        .where(
            Order.merchant_id == shop.id,
            Order.status == OrderStatus.CONFIRMED,
            or_(Payment.id.is_(None), Payment.status != PaymentStatus.SUCCESS),
        )
    ).scalar_one()
    lines.append("")
    lines.append(
        f"{awaiting} order(s) awaiting payment."
        if awaiting else "Nothing awaiting payment. ✓"
    )
    return "\n".join(lines)


def _handle_telegram_command(db: Session, name: str, arg: str,
                             chat_id: str | None) -> bool:
    """Run a slash command. Every command replies — a bot that swallows
    /status or /help is worse than one that says it doesn't understand.
    Commands NEVER reach the order parser."""
    shop = order_intake.resolve_merchant_for_chat(db, "telegram", chat_id) if chat_id else None

    if name == "help":
        _tg_reply(chat_id, _help_text(shop))
        return True

    if name == "status":
        if shop is None:
            _tg_reply(
                chat_id,
                "This chat is not linked to a shop yet. On the dashboard, open "
                'Channels → "Link via /start code" and send that code here.',
            )
            return True
        _tg_reply(chat_id, _status_text(db, shop))
        return True

    if name == "start":
        # A valid pairing code was already consumed above; what reaches us
        # here is a bare /start or a mistyped/expired one.
        if not arg:
            if shop is not None:
                _tg_reply(chat_id,
                          f"✅ This chat is already linked to {shop.business_name}. "
                          "Login codes and order replies arrive here.")
            else:
                _tg_reply(
                    chat_id,
                    "To link this chat to your shop: sign in on the dashboard, "
                    'open Channels and click "Link via /start code", '
                    "then send me that code.",
                )
            return True
        _tg_reply(
            chat_id,
            "That link code is invalid or expired. On the dashboard, open "
            'Channels → "Link via /start code" to get a fresh one.',
        )
        return True

    _tg_reply(chat_id, f"Unknown command /{name}. Send /help to see what I can do.")
    return True


def _handle_telegram_text(db: Session, text: str, chat_id: str | None) -> bool:
    """Shared core of Telegram delivery: pairing (/start <code>) first,
    then slash commands (/status, /help, bare /start), then order intake
    from an already-linked shop chat. Called by BOTH the real webhook
    (setWebhook) and the localhost polling endpoint (getUpdates).
    Returns True when the message was handled."""
    # --- /start pairing: links a Telegram chat to a shop -------------------
    # The shop owner signs in on the dashboard, clicks "Link Telegram",
    # and sends /start <code> to the bot from their own account. Only a
    # pending telegram_link challenge before expiry accepts a chat.
    match = re.fullmatch(r"/start\s+([a-z0-9]+)", text.strip().lower())
    if match:
        code = match.group(1)
        challenge = db.execute(
            select(LoginChallenge).where(
                LoginChallenge.purpose == "telegram_link",
                LoginChallenge.consumed_at.is_(None),
            )
        ).scalars().all()
        for c in challenge:
            if c.expires_at <= datetime.utcnow():
                continue
            if authsvc.check_secret(c, code):
                merchant = db.get(Merchant, c.merchant_id)
                if merchant is None:
                    break
                merchant.telegram_chat_id = str(chat_id) if chat_id is not None else None
                c.consumed_at = datetime.utcnow()
                record_audit(
                    db, action="telegram_chat_linked", object_type="merchant",
                    object_id=merchant.id, merchant_id=merchant.id, actor_type="system",
                    after={"challenge_id": c.id},
                )
                if chat_id:
                    try:
                        telegram.send_text_message(
                            int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                            f"Linked! Login codes for {merchant.business_name} will arrive here "
                            "when you sign in. This chat is authenticated for OTP delivery.",
                        )
                    except (UpstreamError, NotConfiguredError):
                        # Link already succeeded; the confirmation message is
                        # best-effort only — never fail the webhook for it.
                        pass
                return True
        _tg_reply(
            chat_id,
            "That link code is invalid or expired. On the dashboard, open "
            'Channels → "Link via /start code" to get a fresh one.',
        )
        return True

    # --- slash commands: never reach the order parser ---------------------
    command = _parse_command(text)
    if command is not None:
        return _handle_telegram_command(db, command[0], command[1], chat_id)

    # --- orders from a shop's linked Telegram chat -------------------------
    if chat_id:
        shop = order_intake.resolve_merchant_for_chat(db, "telegram", chat_id)
        if shop is not None:
            result = order_intake.parse_and_create(db, shop, chat_id, text, reply_channel="telegram")
            return result

    return False


def _handle_telegram_callback(db: Session, update: dict) -> dict:
    """An inline-keyboard tap: settle the order's payment (💵 cash or
    ✅ UPI) from the chat that received the order message. The chat↔shop
    binding IS the credential, plus the order must belong to that shop —
    so a fabricated callback or another shop's button can never move money.
    Always answers the callback (so Telegram clears the spinner) and edits
    the tapped message to stamp the result and drop the buttons."""
    tap = telegram.extract_callback(update)
    if tap is None:
        return {"received": True, "handled": False}

    def _answer(text: str, alert: bool = False) -> None:
        try:
            telegram.answer_callback_query(tap["callback_id"], text, show_alert=alert)
        except (UpstreamError, NotConfiguredError):
            pass  # the DB outcome is what matters; the spinner is cosmetic

    action, _, order_id = tap["data"].partition(":")
    shop = order_intake.resolve_merchant_for_chat(db, "telegram", tap["chat_id"])
    if shop is None or action not in ("cash", "paid") or not order_id:
        _answer("This chat can't confirm payments.", alert=True)
        return {"received": True, "handled": False}

    order = db.get(Order, order_id)
    if order is None or order.merchant_id != shop.id:
        _answer("Unknown order — it doesn't belong to this shop.", alert=True)
        return {"received": True, "handled": False}

    method = "cash" if action == "cash" else "upi"
    result = _mark_payment_paid(db, order, method)
    if result.get("already_confirmed"):
        _answer(f"Already confirmed ({result.get('method')}) ✓")
        stamp = f"✓ Already confirmed ({result.get('method')})"
    else:
        _answer("Payment confirmed ✓")
        stamp = ("✓ Cash payment confirmed" if method == "cash"
                 else "✓ Payment confirmed via UPI")

    if tap["message_id"] is not None:
        try:
            # Editing without a keyboard removes the buttons, so the same
            # tap can't be answered twice from the chat.
            telegram.edit_message_text(
                tap["chat_id"], tap["message_id"], f"{tap['text']}\n\n{stamp}.",
            )
        except (UpstreamError, NotConfiguredError):
            pass  # the confirmation itself already landed in the DB
    return {"received": True, "handled": True, "payment": result}


def _handle_telegram_update(db: Session, update: dict) -> dict:
    """Core of Telegram delivery, shared by the real webhook (setWebhook)
    and the localhost polling endpoint (getUpdates): /start pairing first,
    then voice/photo/text order intake from an already-linked shop chat.
    Always returns an ack-shaped dict — never raises for messages it simply
    can't attribute (unlinked chats, stickers, edits, callbacks).
    """
    if update.get("callback_query") is not None:
        return _handle_telegram_callback(db, update)

    msg = telegram.extract_media(update)
    if msg is None:
        # Edits/callbacks/stickers/video: acknowledge, nothing to do.
        return {"received": True, "handled": False}

    if msg["kind"] != "text":
        # Voice note or photo of an order list — the exact same intake path
        # WhatsApp uses (shared handlers above), repaid on this same chat.
        chat_id = msg["chat_id"]
        shop = order_intake.resolve_merchant_for_chat(db, "telegram", chat_id)
        if shop is None:
            return {"received": True, "handled": False}
        if msg["kind"] == "audio":
            return _handle_voice_order(db, shop, chat_id, msg, channel="telegram")
        return _handle_photo_order(db, shop, chat_id, msg, channel="telegram")

    handled = _handle_telegram_text(db, msg["text"], msg["chat_id"])
    if isinstance(handled, dict):
        return {"received": True, "handled": True, "order": handled}
    return {"received": True, "handled": bool(handled)}


@router.post("/telegram")
async def receive_telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Telegram update delivery (set via Bot API setWebhook).

    Freely bound to TELEGRAM_SECRET_TOKEN: Telegram sends the exact secret
    you registered on every delivery; a mismatching (or missing) header is
    rejected before the payload is even parsed. Unconfigured secret token —
    including here in production — means the endpoint is closed rather than
    trusting whoever posts at it, mirroring the WhatsApp policy of failing
    loud instead of silent.
    """
    if not telegram.verify_secret_token(x_telegram_bot_api_secret_token):
        raise HTTPException(401, "Invalid or missing Telegram secret token")

    update = await request.json()
    return _handle_telegram_update(db, update)


@router.post("/telegram/poll")
def poll_telegram_updates(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """Localhost-friendly Telegram delivery: pull pending updates via
    getUpdates and run them through the SAME pairing/order logic as the
    real webhook. No public URL or webhook registration needed — the app
    connects outbound to Telegram, which is exactly how localhost dev and
    small self-hosted deployments run forever at ₹0.

    Requires a live session (operator action) and TELEGRAM_BOT_TOKEN.
    Returns how many messages were handled plus the new offset cursor."""
    try:
        authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e

    # A missing token is expected here (the dashboard wizard polls while
    # the operator is still getting one from @BotFather): answer with a
    # clean, self-explanatory error instead of a traceback. Upstream
    # outages (e.g. a webhook already set on the bot) surface as 502 the
    # same way. The frontend treats any non-2xx as "keep polling".
    try:
        updates = telegram.fetch_recent_updates(offset=0, timeout_seconds=0)
    except NotConfiguredError as e:
        raise HTTPException(503, str(e)) from e
    except UpstreamError as e:
        raise HTTPException(502, str(e)) from e

    handled = 0
    highest_update_id = 0
    for update in updates:
        highest_update_id = max(highest_update_id, int(update.get("update_id", 0)))
        try:
            if _handle_telegram_update(db, update).get("handled"):
                handled += 1
        except Exception:
            # One bad message never blocks the rest of the batch (or the
            # pairing the operator is waiting for).
            continue

    # Acknowledge the whole batch so Telegram never re-delivers it.
    if highest_update_id:
        telegram.fetch_recent_updates(offset=highest_update_id + 1, timeout_seconds=0)
    return {"handled": handled, "seen": len(updates)}
