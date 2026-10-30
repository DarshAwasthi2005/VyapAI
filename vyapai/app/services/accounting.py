"""
Simple single-entry bookkeeping core. This is the one part of the system
that must never be "roughly right" — every function here is deterministic,
has no external dependency, and is covered by tests/test_accounting.py.

ONE business event = ONE ledger row. A sale row records money IN (`Sales`,
a credit), a purchase row records money OUT (`Purchase`, a debit), each
with the amount the merchant actually moved (value + GST) and the GST rate
kept as a column so tax reports stay computable later.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.enums import EntryType

ROUNDING = 2  # paise-level rounding for INR


@dataclass
class LedgerLine:
    account: str
    entry_type: EntryType
    amount: float
    gst_rate: float | None = None
    memo: str | None = None


@dataclass
class LedgerTransaction:
    transaction_group: str
    lines: list[LedgerLine] = field(default_factory=list)
    source: str = "manual"


def build_sale_transaction(
    *, taxable_value: float, gst_rate: float, party: str, source: str, memo: str | None = None
) -> LedgerTransaction:
    """A single sale = ONE ledger row: money the merchant received
    (item value + GST). The tax rate is kept on the row so output GST can
    be recomputed for filings without a second row."""
    if taxable_value <= 0:
        raise ValueError("taxable_value must be positive")
    if gst_rate < 0:
        raise ValueError("gst_rate cannot be negative")

    gst_amount = round(taxable_value * gst_rate / 100, ROUNDING)
    total = round(taxable_value + gst_amount, ROUNDING)

    return LedgerTransaction(
        transaction_group=str(uuid.uuid4()),
        lines=[LedgerLine("Sales", EntryType.CREDIT, total, gst_rate, memo or f"Sale to {party}")],
        source=source,
    )


def build_purchase_transaction(
    *, taxable_value: float, gst_rate: float, party: str, source: str, memo: str | None = None
) -> LedgerTransaction:
    """A single purchase = ONE ledger row: money the merchant paid
    (item value + GST), with the GST rate kept for input-credit reports."""
    if taxable_value <= 0:
        raise ValueError("taxable_value must be positive")

    gst_amount = round(taxable_value * gst_rate / 100, ROUNDING)
    total = round(taxable_value + gst_amount, ROUNDING)

    return LedgerTransaction(
        transaction_group=str(uuid.uuid4()),
        lines=[LedgerLine("Purchase", EntryType.DEBIT, total, gst_rate, memo or f"Purchase from {party}")],
        source=source,
    )


def reconcile_input_tax_credit(purchase_gst_amounts: list[float], output_gst_amounts: list[float]) -> dict:
    """Net GST payable = Output GST collected - Input GST Credit available.
    Returns a breakdown, never a single opaque number, so a human can check it."""
    total_input = round(sum(purchase_gst_amounts), ROUNDING)
    total_output = round(sum(output_gst_amounts), ROUNDING)
    net_payable = round(total_output - total_input, ROUNDING)
    return {
        "total_input_tax_credit": total_input,
        "total_output_gst": total_output,
        "net_gst_payable": max(net_payable, 0.0),
        "carried_forward_credit": max(-net_payable, 0.0),
    }


def verify_line_items_sum_to_total(
    line_items: list[tuple[float, float, float]], stated_total: float, tolerance: float = 0.5
) -> tuple[bool, float]:
    """Cross-checks (quantity, unit_price, amount) triples against a stated
    total. Used by the guardrail agent before any bill/voice-note entry is
    posted to the ledger. tolerance in INR to allow for rounding."""
    computed = round(sum(qty * price for qty, price, _ in line_items), ROUNDING)
    stated_items_sum = round(sum(amount for _, _, amount in line_items), ROUNDING)
    ok = abs(computed - stated_items_sum) <= tolerance and abs(stated_items_sum - stated_total) <= tolerance
    return ok, computed
