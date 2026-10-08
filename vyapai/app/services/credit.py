"""
Builds the standardized loan-readiness packet from ledger data, before it
goes to the OCEN adapter. Kept separate from app/integrations/ocen.py so the
"what does a good packet look like" logic can be tested without touching
the network layer.
"""
from __future__ import annotations

from datetime import datetime

from app.integrations.ocen import CashFlowSummary


def build_cash_flow_summary(
    *,
    merchant_id: str,
    period_start: datetime,
    period_end: datetime,
    monthly_inflows: list[float],
    monthly_outflows: list[float],
    gst_filed_turnover_last_12m: float,
    outstanding_receivables: float,
    outstanding_payables: float,
    bounce_count_last_6m: int,
    requested_amount: float,
    purpose: str,
    supporting_doc_refs: list[str] | None = None,
) -> CashFlowSummary:
    if not monthly_inflows or not monthly_outflows:
        raise ValueError("Need at least one month of cash flow data to build a packet")

    avg_in = sum(monthly_inflows) / len(monthly_inflows)
    avg_out = sum(monthly_outflows) / len(monthly_outflows)

    return CashFlowSummary(
        merchant_id=merchant_id,
        period_start=period_start.date().isoformat(),
        period_end=period_end.date().isoformat(),
        monthly_avg_inflow=round(avg_in, 2),
        monthly_avg_outflow=round(avg_out, 2),
        gst_filed_turnover_last_12m=gst_filed_turnover_last_12m,
        outstanding_receivables=outstanding_receivables,
        outstanding_payables=outstanding_payables,
        bounce_count_last_6m=bounce_count_last_6m,
        requested_amount=requested_amount,
        purpose=purpose,
        supporting_doc_refs=supporting_doc_refs or [],
    )


def loan_readiness_score(packet: CashFlowSummary) -> dict:
    """A transparent, explainable pre-screen — NOT a credit decision.
    The actual underwriting happens on the lender's (LA's) side once the
    packet is submitted over OCEN. This just flags obvious red flags early
    so a merchant isn't sent into a rejection needlessly."""
    flags = []
    net_monthly = packet.monthly_avg_inflow - packet.monthly_avg_outflow

    if net_monthly <= 0:
        flags.append("Average monthly outflow exceeds inflow")
    if packet.bounce_count_last_6m > 3:
        flags.append(f"{packet.bounce_count_last_6m} bounced payments in the last 6 months")
    if packet.requested_amount > packet.monthly_avg_inflow * 6:
        flags.append("Requested amount exceeds 6x average monthly inflow")
    if packet.gst_filed_turnover_last_12m <= 0:
        flags.append("No GST-filed turnover on record — lenders will likely ask for more docs")

    return {
        "ready_to_submit": len(flags) == 0,
        "flags": flags,
        "estimated_monthly_surplus": round(net_monthly, 2),
    }
