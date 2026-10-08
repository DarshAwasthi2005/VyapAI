"""
Central configuration. All external-service configuration is optional at
import time — adapters check for their own required vars and raise
NotConfiguredError at call time, not at import time, so the app can boot
and serve health checks / docs even before every integration is wired up.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


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

    # WhatsApp Cloud API
    whatsapp_token: str | None = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str | None = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_verify_token: str | None = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))

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
