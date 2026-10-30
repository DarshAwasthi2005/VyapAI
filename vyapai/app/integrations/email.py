"""Email adapter — the second login identity's OTP delivery channel.

Pluggable like telegram.py / whatsapp.py: a REAL plain-SMTP send (stdlib
`smtplib`, no new dependency) that works with any provider — a Gmail
app-password on a free account is enough, matching VyapAI's zero-cost
blueprint. NotConfiguredError until SMTP_HOST + SMTP_FROM are set; the
caller (services/auth.deliver_login_otp) falls back to dev_echo in
development, exactly like the Telegram path.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError


def _require_config() -> None:
    if not (settings.smtp_host and settings.smtp_from):
        raise NotConfiguredError(
            "Email delivery",
            ["SMTP_HOST", "SMTP_FROM", "(SMTP_USER + SMTP_PASSWORD for authenticated relays)"],
        )


def send_text_email(to_address: str, subject: str, body: str) -> dict:
    """Send a plain-text email via SMTP. TLS automatically: STARTTLS on
    587, SMTP_SSL on 465, opportunistic STARTTLS otherwise."""
    _require_config()
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or ""
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.send_message(msg)  # SMTP_SSL implies authenticated TLS
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if settings.smtp_user and settings.smtp_password:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise UpstreamError(f"Could not send email via SMTP: {e}") from e
    return {"sent": True, "to": to_address}
