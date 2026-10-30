from __future__ import annotations

from pydantic import BaseModel, Field


class MerchantCreate(BaseModel):
    """A shop signs up with a phone number OR an email address — at least
    one identity is required (the service enforces it)."""
    business_name: str
    gstin: str | None = None
    whatsapp_number: str | None = None
    email: str | None = None
    upi_vpa: str | None = None
    preferred_language: str = "hi"
    pin: str | None = Field(
        None,
        description="4–8 digit login PIN. Required (service enforces).",
    )


class MerchantOut(BaseModel):
    # auth fields deliberately excluded — the PIN hash never leaves the DB
    # and neither does the Telegram chat id.
    id: str
    business_name: str
    gstin: str | None
    whatsapp_number: str | None
    email: str | None
    upi_vpa: str | None
    preferred_language: str
    class Config:
        from_attributes = True


class LanguageUpdatePayload(BaseModel):
    """Change the merchant's preferred language (dashboard / agent replies).
    The service validates the code against the supported set."""
    preferred_language: str = Field(min_length=2, max_length=5)


class LoginRequestPayload(BaseModel):
    """Legacy step-1: identify the business by its WhatsApp number.
    Prefer /b2b/auth/otp/request, which covers phone AND email."""
    whatsapp_number: str


# --- unified OTP login (phone | email → code → dashboard) ----------------------

class OtpRequestPayload(BaseModel):
    """Ask for a login/code step on the chosen identity: phone number or
    email. A random 6-digit OTP is generated and delivered to that
    identity's channel; it comes back in the response ONLY as dev_echo."""
    channel: str = Field(pattern="^(phone|email)$")
    value: str = Field(min_length=3, max_length=80)


class OtpVerifyPayload(BaseModel):
    """Present the OTP. No PIN: the OTP alone proves this identity."""
    challenge_id: str
    code: str = Field(min_length=4, max_length=8)


class LoginVerifyPayload(BaseModel):
    """Step 2: present the OTP and the PIN."""
    challenge_id: str
    otp: str
    pin: str


class SessionOut(BaseModel):
    token: str
    merchant: MerchantOut
    delivery: str = Field(description="how the OTP reached you (email | telegram | dev_echo)")


class TextBillEntryRequest(BaseModel):
    """For merchants typing an entry directly (bypassing voice/vision)."""
    merchant_id: str
    party_name: str
    transaction_kind: str = Field(pattern="^(sale|purchase)$")
    taxable_value: float
    gst_rate: float = 0.0


class LedgerEntryOut(BaseModel):
    transaction_group: str
    guardrail_passed: bool
    guardrail_reasons: list[str]
    needs_human_approval: bool


class ApprovalDecisionRequest(BaseModel):
    approve: bool
    note: str | None = None


class LoanReadinessRequest(BaseModel):
    merchant_id: str
    requested_amount: float
    purpose: str


class WhatsAppOrderWebhook(BaseModel):
    """Simplified shape — real Meta webhook payload is nested deeper; see
    api/webhooks.py for the unwrapping logic.

    Offer: the person making the bill picks EITHER a percent discount OR a
    flat cash amount — offer_type carries which, offer_value the number.
    Unknown/absent offers are simply ignored."""
    from_number: str
    message_text: str
    offer_type: str | None = Field(default=None, pattern="^(percent|cash)$")
    offer_value: float | None = Field(default=None, ge=0)
