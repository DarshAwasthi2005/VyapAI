"""Real merchant login: PIN + Telegram OTP + bearer sessions.

Design (all zero-cost, no third-party auth SDK):
- The PIN is hashed with PBKDF2-HMAC-SHA256 (stdlib hashlib — no extra
  dependency) and a per-merchant salt. Timing-safe comparisons everywhere.
- An OTP is a 6-digit code; only its sha256 digest is persisted.
- Delivery: Telegram bot (free, works whenever TELEGRAM_BOT_TOKEN is set and
  the merchant has linked their chat via /start pairing). Until Telegram is
  configured, APP_ENV=development echoes the OTP back in the API response so
  the flow is testable; production refuses to log in rather than
  silently weakening this.
- Sessions: opaque bearer tokens (secrets.token_urlsafe(32)), declared
  lifetime, stored as sha256 digests only.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from app.config import settings
from app.db.models import AuthSession, LoginChallenge, Merchant
from app.integrations.errors import NotConfiguredError, UpstreamError

_PIN_SALT_BYTES = 16          # 128-bit salt
_PBKDF2_ITERATIONS = 200_000  # OWASP-calibrated cost for sha256
_MAX_PIN_ATTEMPTS = 5
_OTP_LENGTH = 6
_OTP_TTL = timedelta(minutes=10)
_PAIRING_TTL = timedelta(minutes=15)


def _to_e164(number: str) -> str:
    """India-style mobile numbers are stored nationally (e.g. "9876543210");
    Twilio SMS needs E.164 ("+919876543210"). Assume the stored value is
    already E.164 when it starts with "+"; otherwise prefix +91."""
    n = number.strip()
    return n if n.startswith("+") else "+91" + n
_SESSION_TTL = timedelta(days=7)


class AuthError(Exception):
    """Raised with a user-facing message; routes translate it to 401/422/503."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- PIN ----------------------------------------------------------------------

def set_pin(merchant: Merchant, pin: str) -> None:
    """Hash and attach a registration/login PIN. Enforces the policy here so
    the API schema and any future signup path stay consistent."""
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        raise AuthError("PIN must be 4–8 digits", status_code=422)
    merchant.pin_salt = secrets.token_hex(_PIN_SALT_BYTES)
    merchant.pin_hash = hashlib.pbkdf2_hmac(
        "sha256", pin.encode(), bytes.fromhex(merchant.pin_salt), _PBKDF2_ITERATIONS,
    ).hex()


def verify_pin(merchant: Merchant, pin: str) -> bool:
    if not merchant.pin_hash or not merchant.pin_salt:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode(), bytes.fromhex(merchant.pin_salt), _PBKDF2_ITERATIONS,
    ).hex()
    return hmac.compare_digest(digest, merchant.pin_hash)


# --- OTP / one-time secrets ---------------------------------------------------

def new_otp() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(_OTP_LENGTH))


def new_pairing_code() -> str:
    # 6 letters/digits, no ambiguous glyphs (0/O, 1/I).
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(_OTP_LENGTH + 2))


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def check_secret(challenge: LoginChallenge, presented: str) -> bool:
    return hmac.compare_digest(_digest(presented), challenge.secret_hash)


def secret_digest(secret: str) -> str:
    return _digest(secret)


# --- Challenges ---------------------------------------------------------------

def challenge_expired(challenge: LoginChallenge, now: datetime) -> bool:
    return challenge.expires_at <= now


def consume(challenge: LoginChallenge, now: datetime) -> None:
    challenge.consumed_at = now


# --- OTP delivery -------------------------------------------------------------

def deliver_otp(merchant: Merchant, otp: str) -> str:
    """Send the OTP to the merchant's linked Telegram chat.

    Returns the delivery channel used: \"telegram\" or \"dev_echo\".
    Raises AuthError(503) when no safe channel exists — an OTP that can't be
    delivered must never be handed out instead.
    """
    from app.config import settings as _s
    from app.integrations import telegram

    if merchant.telegram_chat_id:
        try:
            from app.integrations.errors import NotConfiguredError, UpstreamError

            telegram.send_text_message(
                merchant.telegram_chat_id,
                f"Your VyapAI login code is {otp}. It expires in 10 minutes.\n"
                "VyapAI will never ask for this code outside the app.",
            )
            return "telegram"
        except (NotConfiguredError, UpstreamError):
            # A linked chat with no runnable bot (no TELEGRAM_BOT_TOKEN on
            # this machine, or Telegram briefly unreachable) must not 500:
            # development falls through to dev_echo below, production keeps
            # refusing loud at the bottom of this function.
            if _s.app_env != "development":
                raise AuthError(
                    "Could not deliver your login code via Telegram. "
                    "Please check TELEGRAM_BOT_TOKEN / try again shortly.",
                    status_code=503,
                )

    if _s.app_env == "development":
        # Dev fallback: no delivery channel exists yet, so the challenge would
        # otherwise be untestable. The response parser must label it clearly
        # as dev-only. Production never gets here (these branches raise).
        return "dev_echo"

    raise AuthError(
        "No way to deliver your login code: link your Telegram chat first "
        "(/start pairing to the bot), or set TELEGRAM_BOT_TOKEN.",
        status_code=503,
    )


# --- OTP delivery by login identity (phone | email) -----------------------------

def deliver_login_otp(merchant: Merchant, otp: str, channel: str) -> str:
    """Send an OTP on the login identity the user chose: phone or email.

    - phone → real SMS via Twilio when configured ("sms"), Telegram when
      the chat is linked+configured, dev_echo otherwise.
    - email → REAL plain-SMTP send when SMTP_* is configured, dev_echo
      otherwise.

    Development fallback: dev_echo on any delivery failure. Production
    refuses loudly (503) — an OTP that can't be delivered is never handed
    out instead. Returns the delivery label used."""
    from app.config import settings as _s

    if channel not in ("phone", "email"):
        raise ValueError(f"unknown otp channel: {channel}")

    if channel == "email":
        from app.integrations import email
        from app.integrations.errors import NotConfiguredError, UpstreamError

        if not merchant.email:
            raise AuthError(
                "This shop was registered with a phone number, not an email",
                status_code=422,
            )
        try:
            email.send_text_email(
                merchant.email,
                "Your VyapAI login code",
                f"Your VyapAI login code is {otp}. It expires in 10 minutes.\n\n"
                "VyapAI will never ask for this code outside the app.",
            )
            return "email"
        except (NotConfiguredError, UpstreamError):
            if _s.app_env != "development":
                raise AuthError(
                    "Could not deliver the login code by email. Please check "
                    "SMTP_HOST / SMTP_FROM on the server and try again.",
                    status_code=503,
                )
            return "dev_echo"

    # Phone identity: prefer real SMS when Twilio is configured — the OTP
    # then lands on the merchant's actual phone even with no Telegram link.
    from app.integrations import sms
    if merchant.whatsapp_number and settings.twilio_account_sid:
        try:
            sms.send_otp_sms(_to_e164(merchant.whatsapp_number), otp)
            return "sms"
        except (NotConfiguredError, UpstreamError):
            if _s.app_env != "development":
                raise AuthError(
                    "Could not deliver the login code by SMS. Please check "
                    "TWILIO_* on the server and try again.",
                    status_code=503,
                )
            # Dev: fall through to Telegram/dev_echo below.

    return deliver_otp(merchant, otp)


# --- Sessions -----------------------------------------------------------------

def create_session(db, merchant_id: str) -> tuple[AuthSession, str]:
    """Create a session. Returns (row, raw_token) — raw_token is returned to
    the caller exactly once and is not stored."""
    raw = secrets.token_urlsafe(32)
    session = AuthSession(
        merchant_id=merchant_id,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.utcnow() + _SESSION_TTL,
    )
    db.add(session)
    db.flush()
    return session, raw


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_bearer(authorization: str) -> str:
    scheme, _, value = (authorization or "").strip().partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise AuthError("Sign in to continue", status_code=401)
    return value


def resolve_session(db, authorization: str) -> tuple[Merchant, AuthSession]:
    """Validate the Authorization header and return (merchant, session).

    401 = no/expired/revoked token, 403 = token belongs to another shop. DB
    stores digests only, so a leaked database cannot mint live sessions."""
    raw = _parse_bearer(authorization)
    from sqlalchemy import select
    session = db.execute(
        select(AuthSession).where(AuthSession.token_hash == token_hash(raw))
    ).scalar_one_or_none()
    if session is None:
        raise AuthError("Session invalid or expired — sign in again", status_code=401)
    now = datetime.utcnow()
    if session.revoked_at is not None or session.expires_at <= now:
        raise AuthError("Session expired — sign in again", status_code=401)
    merchant = db.get(Merchant, session.merchant_id)
    if merchant is None:
        raise AuthError("Session invalid — sign in again", status_code=401)
    return merchant, session


def revoke_session(db, session: AuthSession) -> None:
    session.revoked_at = datetime.utcnow()
    db.flush()
