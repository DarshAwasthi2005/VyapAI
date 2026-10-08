"""
OCEN (Open Credit Enablement Network) adapter.

VyapAI plays the role of an LSP (Loan Service Provider) — it aggregates a
merchant's cash-flow data into a standardized loan-readiness packet and
submits it to lending platforms (LAs) over the OCEN API spec. Becoming an
empanelled LSP requires a formal agreement with an OCEN network operator
(e.g. Yubi/Setu/Sahamati) — this adapter assumes that empanelment exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError


@dataclass
class CashFlowSummary:
    """Standardized loan-readiness packet built from the merchant ledger."""
    merchant_id: str
    period_start: str
    period_end: str
    monthly_avg_inflow: float
    monthly_avg_outflow: float
    gst_filed_turnover_last_12m: float
    outstanding_receivables: float
    outstanding_payables: float
    bounce_count_last_6m: int
    requested_amount: float
    purpose: str
    supporting_doc_refs: list[str] = field(default_factory=list)


@dataclass
class LoanApplicationResult:
    application_id: str
    status: str  # "submitted" | "rejected" | "needs_more_docs"
    la_partner: str
    raw: dict


def submit_loan_application(packet: CashFlowSummary) -> LoanApplicationResult:
    if not (settings.ocen_lsp_id and settings.ocen_base_url and settings.ocen_api_key):
        raise NotConfiguredError(
            "OCEN", ["OCEN_LSP_ID", "OCEN_BASE_URL", "OCEN_API_KEY"]
        )

    # Sanity checks a real underwriter would also run — fail fast before
    # spending an API call or, worse, submitting bad data to a lender.
    if packet.requested_amount <= 0:
        raise ValueError("requested_amount must be positive")
    if packet.monthly_avg_inflow < 0 or packet.monthly_avg_outflow < 0:
        raise ValueError("cash flow figures cannot be negative")

    # TODO(deploy): real OCEN LSP->LA submission, e.g.
    #   resp = httpx.post(f"{settings.ocen_base_url}/lspLa/v2/loans/application",
    #                      headers={"Authorization": f"Bearer {settings.ocen_api_key}",
    #                               "lspId": settings.ocen_lsp_id},
    #                      json=packet_to_ocen_schema(packet), timeout=20)
    #   resp.raise_for_status()
    #   data = resp.json()
    #   return LoanApplicationResult(data["applicationId"], data["status"], data["laId"], data)
    raise UpstreamError(
        "OCEN submission is stubbed pending LSP empanelment + network operator "
        "integration. This action must always require human-in-the-loop "
        "approval — never auto-submit a merchant's loan application."
    )
