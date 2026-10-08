from __future__ import annotations

from pydantic import BaseModel, Field


class MerchantCreate(BaseModel):
    business_name: str
    gstin: str | None = None
    whatsapp_number: str | None = None
    upi_vpa: str | None = None
    preferred_language: str = "hi"


class MerchantOut(BaseModel):
    id: str
    business_name: str
    gstin: str | None
    whatsapp_number: str | None
    upi_vpa: str | None
    preferred_language: str

    class Config:
        from_attributes = True


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
    api/webhooks.py for the unwrapping logic."""
    from_number: str
    message_text: str
