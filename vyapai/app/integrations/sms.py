"""
SMS adapter — the "real OTP on my phone" channel for phone-identity logins.

Twilio is wired via its plain REST API (httpx, no SDK dependency), mirroring
the telegram.py adapter interface so delivery stays pluggable. Real sends
work the moment the three TWILIO_* env vars are set:
  TWILIO_ACCOUNT_SID    (AC... — console dashboard)
  TWILIO_AUTH_TOKEN     (console dashboard)
  TWILIO_PHONE_NUMBER   (trial sender number, E.164 e.g. +1XXXXXXXXXX or
                         an India sender +91XXXXXXXXXX)

Unconfigured adapter raises NotConfiguredError at call time (never at import
time) so the app boots cleanly without it; callers fall back to the next
channel exactly like Telegram/email do.
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

_BASE = "https://api.twilio.com/2010-04-01"


def _credentials() -> tuple[str, str, str]:
    if not (settings.twilio_account_sid and settings.twilio_auth_token
            and settings.twilio_phone_number):
        raise NotConfiguredError(
            "Twilio SMS",
            ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
        )
    return (settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_phone_number)


def send_otp_sms(to_number_e164: str, otp: str) -> None:
    """Send a login OTP over SMS. Raises UpstreamError on any Twilio/API
    failure so the caller can fall back (or refuse loudly in production).
    Phone numbers must be E.164 (+<countrycode><number>)."""
    sid, token, from_number = _credentials()
    try:
        resp = httpx.post(
            f"{_BASE}/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={
                "To": to_number_e164,
                "From": from_number,
                "Body": f"Your VyapAI login code is {otp}. It expires in 10 minutes.\n"
                        "VyapAI will never ask for this code outside the app.",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.RequestError) as e:
        raise UpstreamError(f"Twilio SMS send failed: {e}") from e
