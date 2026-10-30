"""Merchant authentication endpoints — the real login, replacing the demo
shop-chip switch. Bearer-token sessions; OTP delivered by Telegram (free)
with a clearly-labelled dev fallback when Telegram is unconfigured."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import LoginChallenge, Merchant
from app.db.session import get_db
from app.schemas import (
    LanguageUpdatePayload,
    LoginRequestPayload,
    LoginVerifyPayload,
    MerchantOut,
    OtpRequestPayload,
    OtpVerifyPayload,
    SessionOut,
)
from app.services import auth as authsvc
from app.services.audit import record_audit

router = APIRouter(prefix="/b2b/auth", tags=["auth"])


def scoped_merchant(merchant_id: str, db: Session, authorization: str) -> Merchant:
    """Shared guard: the request must carry a live session for THIS shop.

    401 = not signed in / session dead, 404 = unknown merchant,
    403 = signed in as a different shop. Every merchant-data endpoint calls
    this — no shop's books are readable by another login.
    """
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")
    try:
        session_merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    if session_merchant.id != merchant_id:
        raise HTTPException(403, "You are signed in as a different shop")
    return merchant


@router.post("/request-login")
def request_login(payload: LoginRequestPayload, db: Session = Depends(get_db)):
    """Step 1: identify the shop by WhatsApp number and start an OTP challenge.

    The OTP is sent to the shop's linked Telegram chat. In dev (no Telegram)
    the response carries it back labelled dev_echo so the flow can be tested.
    """
    merchant = db.execute(
        select(Merchant).where(Merchant.whatsapp_number == payload.whatsapp_number)
    ).scalar_one_or_none()
    if not merchant or not merchant.pin_hash:
        # Same error for "no such shop" and "shop never set a PIN": does not
        # reveal whether a number is registered.
        raise HTTPException(404, "No shop found with that number and PIN set up")

    otp = authsvc.new_otp()
    challenge = LoginChallenge(
        merchant_id=merchant.id,
        purpose="login",
        secret_hash=authsvc.secret_digest(otp),
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    challenge.delivery = authsvc.deliver_otp(merchant, otp)  # may raise 503
    db.add(challenge)
    db.flush()

    record_audit(
        db, action="login_requested", object_type="login_challenge", object_id=challenge.id,
        merchant_id=merchant.id, actor_type="api",
        after={"delivery": challenge.delivery},
    )
    return {
        "challenge_id": challenge.id,
        "delivery": challenge.delivery,
        "expires_in_seconds": 600,
        # dev-only convenience: real deployments never receive this
        **({"otp": otp} if challenge.delivery == "dev_echo" else {}),
    }


@router.post("/verify", response_model=SessionOut)
def verify(payload: LoginVerifyPayload, db: Session = Depends(get_db)):
    """Step 2: prove it. OTP + PIN must both check out; anything else burns an
    attempt (5 max) and never says which factor failed."""
    challenge = db.get(LoginChallenge, payload.challenge_id)
    now = datetime.utcnow()
    if not challenge or challenge.purpose != "login":
        raise HTTPException(401, "Login request not found — start again")
    if challenge.consumed_at is not None or challenge.expires_at <= now:
        raise HTTPException(401, "This code has expired — request a new one")
    if challenge.attempts >= challenge.max_attempts:
        challenge.consumed_at = now
        db.commit()  # persist the exhaustion a rollback would otherwise erase
        raise HTTPException(401, "Too many wrong attempts — request a new code")

    challenge.attempts += 1

    merchant = db.get(Merchant, challenge.merchant_id)
    if not authsvc.check_secret(challenge, payload.otp) or not authsvc.verify_pin(merchant, payload.pin):
        record_audit(
            db, action="login_failed", object_type="login_challenge", object_id=challenge.id,
            merchant_id=merchant.id if merchant else None, actor_type="api",
            after={"attempts": challenge.attempts},
        )
        # Failed attempts MUST survive the HTTPException-induced request
        # rollback — otherwise the 5-attempt ceiling can be retried forever.
        db.commit()
        raise HTTPException(401, "Wrong code or PIN")

    authsvc.consume(challenge, now)
    _session, token = authsvc.create_session(db, merchant.id)
    record_audit(
        db, action="login_succeeded", object_type="auth_session", object_id=_session.id,
        merchant_id=merchant.id, actor_type="api", after={"challenge_id": challenge.id},
    )
    return SessionOut(token=token, merchant=MerchantOut.model_validate(merchant), delivery=challenge.delivery)


@router.post("/logout")
def logout(db: Session = Depends(get_db), authorization: str = Header(default="")):
    try:
        _merchant, session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        # Sign-out of a dead session is idempotent from the caller's view —
        # the dashboard "sign out" button must never 500.
        raise HTTPException(e.status_code, e.message) from e
    authsvc.revoke_session(db, session)
    record_audit(
        db, action="logout", object_type="auth_session", object_id=session.id,
        merchant_id=session.merchant_id, actor_type="api", after={"revoked": True},
    )
    return {"status": "signed_out"}


_SUPPORTED_LANGUAGES = {
    "hi": "हिन्दी", "en": "English", "gu": "ગુજરાતી", "mr": "मराठी",
    "bn": "বাংলা", "ta": "தமிழ்", "te": "తెలుగు", "kn": "ಕನ್ನಡ", "pa": "ਪੰਜਾਬੀ",
}


@router.get("/me", response_model=MerchantOut)
def me(db: Session = Depends(get_db), authorization: str = Header(default="")):
    try:
        merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    return MerchantOut.model_validate(merchant)


@router.patch("/me/language", response_model=MerchantOut)
def change_language(
    payload: LanguageUpdatePayload,
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Switch the shop's preferred language from the signed-in dashboard.

    This setting drives which language the agent uses when talking to the
    merchant and their customers (confirmations, reminders, receipts).
    Session-scoped: it changes THIS shop and no other."""
    try:
        merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    code = payload.preferred_language.strip().lower()
    if code not in _SUPPORTED_LANGUAGES:
        raise HTTPException(
            422,
            "Unsupported language " + payload.preferred_language
            + " — supported: " + ", ".join(f"{c} ({n})" for c, n in sorted(_SUPPORTED_LANGUAGES.items())),
        )
    old = merchant.preferred_language
    merchant.preferred_language = code
    record_audit(
        db, action="language_changed", object_type="merchant", object_id=merchant.id,
        merchant_id=merchant.id, actor_type="merchant",
        before={"preferred_language": old}, after={"preferred_language": code},
    )
    db.commit()
    return MerchantOut.model_validate(merchant)


# --- unified OTP login: phone OR email → code → dashboard -----------------------

@router.post("/otp/request")
def request_otp(payload: OtpRequestPayload, db: Session = Depends(get_db)):
    """Login/identity step the user picked: phone number or email.

    Generates a fresh 6-digit OTP, delivers it to THAT identity (phone →
    free messaging channel; email → SMTP when configured), returns the
    challenge. The plaintext OTP is echoed ONLY when delivery fell back to
    dev_echo — never when it genuinely went out."""
    value = payload.value.strip()
    if payload.channel == "email":
        merchant = db.execute(
            select(Merchant).where(func.lower(Merchant.email) == value.lower())
        ).scalar_one_or_none()
    else:
        merchant = db.execute(
            select(Merchant).where(Merchant.whatsapp_number == value)
        ).scalar_one_or_none()
    if not merchant:
        raise HTTPException(404, "No shop found with that phone number or email")

    otp = authsvc.new_otp()
    challenge = LoginChallenge(
        merchant_id=merchant.id,
        purpose="login",
        secret_hash=authsvc.secret_digest(otp),
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    challenge.delivery = authsvc.deliver_login_otp(merchant, otp, payload.channel)  # may raise 503/422
    db.add(challenge)
    db.flush()

    record_audit(
        db, action="login_requested", object_type="login_challenge", object_id=challenge.id,
        merchant_id=merchant.id, actor_type="api",
        after={"delivery": challenge.delivery, "channel": payload.channel},
    )
    return {
        "challenge_id": challenge.id,
        "delivery": challenge.delivery,
        "sent_to": merchant.email if payload.channel == "email" else merchant.whatsapp_number,
        "expires_in_seconds": 600,
        # dev-only convenience: real deployments never receive this
        **({"otp": otp} if challenge.delivery == "dev_echo" else {}),
    }


@router.post("/otp/verify", response_model=SessionOut)
def verify_otp(payload: OtpVerifyPayload, db: Session = Depends(get_db)):
    """Prove it with the code alone — no PIN. Same single-use, expiry and
    5-attempt ceiling as the legacy verify; a wrong attempt burns one."""
    challenge = db.get(LoginChallenge, payload.challenge_id)
    now = datetime.utcnow()
    if not challenge or challenge.purpose != "login":
        raise HTTPException(401, "Login request not found — start again")
    if challenge.consumed_at is not None or challenge.expires_at <= now:
        raise HTTPException(401, "This code has expired — request a new one")
    if challenge.attempts >= challenge.max_attempts:
        challenge.consumed_at = now
        db.commit()  # persist the exhaustion a rollback would otherwise erase
        raise HTTPException(401, "Too many wrong attempts — request a new code")

    challenge.attempts += 1

    merchant = db.get(Merchant, challenge.merchant_id)
    if not authsvc.check_secret(challenge, payload.code):   # wrong code burns an attempt
        record_audit(
            db, action="login_failed", object_type="login_challenge", object_id=challenge.id,
            merchant_id=challenge.merchant_id, actor_type="api",
            after={"attempts": challenge.attempts},
        )
        db.commit()  # failed counters MUST survive the HTTPException rollback
        raise HTTPException(401, "Wrong code")

    authsvc.consume(challenge, now)
    _session, token = authsvc.create_session(db, merchant.id)
    record_audit(
        db, action="login_succeeded", object_type="auth_session", object_id=_session.id,
        merchant_id=merchant.id, actor_type="api", after={"challenge_id": challenge.id},
    )
    return SessionOut(token=token, merchant=MerchantOut.model_validate(merchant), delivery=challenge.delivery)


@router.post("/telegram/link")
def request_telegram_link(
    db: Session = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Ask the bot for a pairing code. Message `/start <code>` to the bot from
    your Telegram account; the webhook then stores your chat_id against this
    shop and future login OTPs arrive there, protected by Telegram delivery.

    This endpoint itself requires a live session (you must be signed in to
    link a chat to a shop) — it is the ONE way to attach Telegram."""
    try:
        merchant, _session = authsvc.resolve_session(db, authorization)
    except authsvc.AuthError as e:
        raise HTTPException(e.status_code, e.message) from e
    code = authsvc.new_pairing_code()
    challenge = LoginChallenge(
        merchant_id=merchant.id,
        purpose="telegram_link",
        secret_hash=authsvc.secret_digest(code),
        expires_at=datetime.utcnow() + timedelta(minutes=15),
    )
    challenge.delivery = "inbound"
    db.add(challenge)
    db.flush()
    bot_name = "the VyapAI bot"
    return {
        "challenge_id": challenge.id,
        "code": code,
        "instructions": f"Open Telegram and send /start {code} to {bot_name} within 15 minutes.",
    }


@router.get("/telegram/link/{challenge_id}")
def check_telegram_link(challenge_id: str, db: Session = Depends(get_db)):
    """Pollable status: did the /start pairing land? Used for dev polling."""
    challenge = db.get(LoginChallenge, challenge_id)
    if not challenge or challenge.purpose != "telegram_link":
        raise HTTPException(404, "Link request not found")
    merchant = db.get(Merchant, challenge.merchant_id)
    linked = bool(merchant and merchant.telegram_chat_id)
    return {"linked": linked, "challenge_status": "consumed" if challenge.consumed_at else "pending"}
