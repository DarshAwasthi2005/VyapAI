"""
Central configuration. All external-service configuration is optional at
import time — adapters check for their own required vars and raise
NotConfiguredError at call time, not at import time, so the app can boot
and serve health checks / docs even before every integration is wired up.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import dotenv

# .env lives beside this package's parent (the project root). Loading here
# means a plain `uvicorn app.main:app` picks up DATABASE_URL etc. without
# requiring the operator to export them by hand. Real environment variables
# always win (override=True is NOT used), matching docker-compose env_file
# semantics.
dotenv.load_dotenv(dotenv.find_dotenv(usecwd=True) or ".env")


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Core
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development"))
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql+psycopg2://vyapai:vyapai@db:5432/vyapai"
        )
    )
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-insecure-key"))

    # Speech (Bhashini / Indic-Whisper)
    bhashini_api_key: str | None = field(default_factory=lambda: _env("BHASHINI_API_KEY"))
    bhashini_endpoint: str | None = field(default_factory=lambda: _env("BHASHINI_ENDPOINT"))
    indic_whisper_endpoint: str | None = field(default_factory=lambda: _env("INDIC_WHISPER_ENDPOINT"))

    # Vision / OCR — free options, tried in this order:
    #   1. Gemini API (hosted): free tier, no credit card, ~3-minute signup at
    #      https://aistudio.google.com/apikey. Best accuracy of the free options
    #      and the simplest to wire up — just one env var.
    #   2. Ollama (self-hosted): no signup and no external network calls at all,
    #      but needs a model pulled locally and is slower / less accurate.
    # See app/integrations/vision.py for the full picture.
    gemini_api_key: str | None = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str | None = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))
    ollama_base_url: str | None = field(
        default_factory=lambda: _env("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    ollama_vision_model: str | None = field(
        default_factory=lambda: _env("OLLAMA_VISION_MODEL", "llama3.2-vision")
    )

    # Groq — FREE tier, no card, ~1-minute signup at https://console.groq.com.
    # Best free hosted option for BOTH text structuring and bill-photo vision:
    # llama-3.2-11b-vision handles images, the text models handle structuring.
    # Unlike Gemini's free tier, Groq documents that inference inputs/outputs
    # are not retained by default — still pseudonymize PII (see docs/
    # ZERO_COST_BLUEPRINT.md) before any hosted call.
    groq_api_key: str | None = field(default_factory=lambda: _env("GROQ_API_KEY"))
    groq_vision_model: str = field(
        default_factory=lambda: _env("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    )
    groq_text_model: str = field(
        default_factory=lambda: _env("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
    )

    # Vosk — self-hosted ASR server, completely free, Apache-2.0 models.
    # Point VOSK_ENDPOINT at a small HTTP wrapper exposing POST /asr
    # {"audio_b64": ...} -> {"text": ...} (see docs/ZERO_COST_BLUEPRINT.md).
    # Hindi small model is 42 MB, Gujarati 100 MB; Tamil is NOT available.
    vosk_endpoint: str | None = field(default_factory=lambda: _env("VOSK_ENDPOINT"))

    # Media storage (voice notes / bill photos). Defaults to the local
    # filesystem backend (MEDIA_BACKEND=local) writing to MEDIA_LOCAL_DIR —
    # zero-config for development. Set MEDIA_BACKEND=r2 and the four R2_*
    # vars to use Cloudflare R2's 10-GB free tier (requires `pip install
    # boto3`, an optional dependency kept out of the default install).
    media_backend: str = field(default_factory=lambda: _env("MEDIA_BACKEND", "local"))
    media_local_dir: str = field(default_factory=lambda: _env("MEDIA_LOCAL_DIR", ".media"))
    r2_account_id: str | None = field(default_factory=lambda: _env("R2_ACCOUNT_ID"))
    r2_access_key_id: str | None = field(default_factory=lambda: _env("R2_ACCESS_KEY_ID"))
    r2_secret_access_key: str | None = field(default_factory=lambda: _env("R2_SECRET_ACCESS_KEY"))
    r2_bucket: str | None = field(default_factory=lambda: _env("R2_BUCKET"))

    # Telegram Bot — the zero-cost messaging channel. A bot token is free
    # forever from @BotFather; TG_SECRET_TOKEN is an optional shared secret
    # Meta-style header (X-Telegram-Bot-Api-Secret-Token) set via
    # setWebhook for verifying inbound updates.
    telegram_bot_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))

    # SMS OTP delivery for phone-identity logins (Twilio REST API).
    # Unconfigured adapter falls back exactly like Telegram/email.
    twilio_account_sid: str | None = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str | None = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_phone_number: str | None = field(default_factory=lambda: _env("TWILIO_PHONE_NUMBER"))
    telegram_secret_token: str | None = field(default_factory=lambda: _env("TELEGRAM_SECRET_TOKEN"))

    # WhatsApp Cloud API
    whatsapp_token: str | None = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str | None = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_verify_token: str | None = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    # App secret used to verify X-Hub-Signature-256 on inbound webhooks.
    # Required (startup fails without it) once APP_ENV=production.
    whatsapp_app_secret: str | None = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))

    # Email OTP delivery (identity option 2: sign in with email). Plain
    # SMTP over the standard library — works with any provider, including
    # a Gmail app-password on a free account; host/port/user/password/from.
    # Unconfigured adapter falls back to dev_echo exactly like Telegram.
    smtp_host: str | None = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str | None = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str | None = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str | None = field(default_factory=lambda: _env("SMTP_FROM"))

    # UPI
    upi_payee_vpa: str | None = field(default_factory=lambda: _env("UPI_PAYEE_VPA"))
    upi_payee_name: str | None = field(default_factory=lambda: _env("UPI_PAYEE_NAME"))

    # GSTN / e-way bill (via GSP)
    gsp_client_id: str | None = field(default_factory=lambda: _env("GSP_CLIENT_ID"))
    gsp_client_secret: str | None = field(default_factory=lambda: _env("GSP_CLIENT_SECRET"))
    gsp_base_url: str | None = field(default_factory=lambda: _env("GSP_BASE_URL"))

    # OCEN
    ocen_lsp_id: str | None = field(default_factory=lambda: _env("OCEN_LSP_ID"))
    ocen_base_url: str | None = field(default_factory=lambda: _env("OCEN_BASE_URL"))
    ocen_api_key: str | None = field(default_factory=lambda: _env("OCEN_API_KEY"))

    # Guardrails
    max_auto_approve_amount_inr: float = field(
        default_factory=lambda: float(_env("MAX_AUTO_APPROVE_AMOUNT_INR", "50000"))
    )


settings = Settings()
