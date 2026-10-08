"""
Double-entry bookkeeping core. This is the one part of the system that must
never be "roughly right" — every function here is deterministic, has no
external dependency, and is covered by tests/test_accounting.py.
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

    def is_balanced(self) -> bool:
        debits = round(sum(l.amount for l in self.lines if l.entry_type == EntryType.DEBIT), ROUNDING)
        credits = round(sum(l.amount for l in self.lines if l.entry_type == EntryType.CREDIT), ROUNDING)
        return debits == credits


class UnbalancedTransactionError(ValueError):
    pass


def build_sale_transaction(
    *, taxable_value: float, gst_rate: float, party: str, source: str, memo: str | None = None
) -> LedgerTransaction:
    """A single sale: Debit Cash/Debtors for (value+GST), Credit Sales for the
    taxable value, Credit Output GST for the tax portion. This is the
    standard Indian GST sales entry shape."""
    if taxable_value <= 0:
        raise ValueError("taxable_value must be positive")
    if gst_rate < 0:
        raise ValueError("gst_rate cannot be negative")

    gst_amount = round(taxable_value * gst_rate / 100, ROUNDING)
    total = round(taxable_value + gst_amount, ROUNDING)
    group = str(uuid.uuid4())

    lines = [
        LedgerLine("Debtors/Cash", EntryType.DEBIT, total, gst_rate, memo or f"Sale to {party}"),
        LedgerLine("Sales", EntryType.CREDIT, taxable_value, gst_rate, memo),
    ]
    if gst_amount > 0:
        lines.append(LedgerLine("Output GST Payable", EntryType.CREDIT, gst_amount, gst_rate, memo))

    txn = LedgerTransaction(transaction_group=group, lines=lines, source=source)
    if not txn.is_balanced():
        raise UnbalancedTransactionError(f"Sale transaction did not balance: {txn.lines}")
    return txn


def build_purchase_transaction(
    *, taxable_value: float, gst_rate: float, party: str, source: str, memo: str | None = None
) -> LedgerTransaction:
    """A purchase (for input tax credit reconciliation): Debit Purchases and
    Input GST Credit, Credit Creditors/Cash."""
    if taxable_value <= 0:
        raise ValueError("taxable_value must be positive")

    gst_amount = round(taxable_value * gst_rate / 100, ROUNDING)
    total = round(taxable_value + gst_amount, ROUNDING)
    group = str(uuid.uuid4())

    lines = [
        LedgerLine("Purchases", EntryType.DEBIT, taxable_value, gst_rate, memo or f"Purchase from {party}"),
    ]
    if gst_amount > 0:
        lines.append(LedgerLine("Input GST Credit", EntryType.DEBIT, gst_amount, gst_rate, memo))
    lines.append(LedgerLine("Creditors/Cash", EntryType.CREDIT, total, gst_rate, memo))

    txn = LedgerTransaction(transaction_group=group, lines=lines, source=source)
    if not txn.is_balanced():
        raise UnbalancedTransactionError(f"Purchase transaction did not balance: {txn.lines}")
    return txn


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
