"""
The guardrail agent is deliberately NOT an LLM call. Its whole job is to be
a boring, deterministic circuit breaker between "an LLM extracted/interpreted
something" and "money moves or a filing gets submitted."

Any of the checks below failing routes the action to the ApprovalRequest
queue (human-in-the-loop) instead of executing it — see
app/db/models.ApprovalRequest and app/agents/graph_b2b.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import settings
from app.integrations.gst import validate_gst_rate
from app.services.accounting import verify_line_items_sum_to_total


@dataclass
class GuardrailResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    requires_human_approval: bool = False


def validate_bill_extraction(
    line_items: list[tuple[float, float, float]],  # (qty, unit_price, amount)
    stated_total: float | None,
    gst_rate: float | None,
) -> GuardrailResult:
    reasons = []

    if not line_items:
        reasons.append("No line items extracted — nothing to post")
        return GuardrailResult(False, reasons, requires_human_approval=True)

    if stated_total is not None:
        ok, computed = verify_line_items_sum_to_total(line_items, stated_total)
        if not ok:
            reasons.append(
                f"Line items sum to {computed} but stated total is {stated_total} — arithmetic mismatch"
            )

    if gst_rate is not None and not validate_gst_rate(gst_rate):
        reasons.append(f"GST rate {gst_rate} is not a recognized slab")

    passed = len(reasons) == 0
    return GuardrailResult(passed=passed, reasons=reasons, requires_human_approval=not passed)


def validate_ledger_entry_amount(amount: float) -> GuardrailResult:
    """Large or negative entries always get a human's eyes before posting."""
    reasons = []
    if amount < 0:
        reasons.append("Negative ledger amount")
    if amount > settings.max_auto_approve_amount_inr:
        reasons.append(
            f"Amount ₹{amount:,.2f} exceeds auto-approve threshold "
            f"₹{settings.max_auto_approve_amount_inr:,.2f}"
        )
    passed = len(reasons) == 0
    return GuardrailResult(passed=passed, reasons=reasons, requires_human_approval=not passed)


def requires_human_approval_for_action(action_type: str) -> bool:
    """Some action types are ALWAYS human-gated, regardless of amount —
    tax filings and credit submissions are not things an agent auto-submits,
    full stop."""
    always_gated = {"e_way_bill", "loan_application", "gst_filing"}
    return action_type in always_gated
