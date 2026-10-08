from app.agents.guardrails import (
    requires_human_approval_for_action,
    validate_bill_extraction,
    validate_ledger_entry_amount,
)


def test_valid_bill_passes():
    result = validate_bill_extraction(
        line_items=[(5.0, 40.0, 200.0), (2.0, 100.0, 200.0)],
        stated_total=400.0,
        gst_rate=18.0,
    )
    assert result.passed
    assert not result.requires_human_approval


def test_arithmetic_mismatch_fails():
    result = validate_bill_extraction(
        line_items=[(5.0, 40.0, 200.0)],
        stated_total=999.0,
        gst_rate=18.0,
    )
    assert not result.passed
    assert result.requires_human_approval
    assert any("mismatch" in r for r in result.reasons)


def test_invalid_gst_rate_fails():
    result = validate_bill_extraction(
        line_items=[(1.0, 100.0, 100.0)],
        stated_total=100.0,
        gst_rate=17.0,  # not a real slab
    )
    assert not result.passed
    assert any("not a recognized slab" in r for r in result.reasons)


def test_empty_line_items_requires_approval():
    result = validate_bill_extraction(line_items=[], stated_total=None, gst_rate=None)
    assert not result.passed
    assert result.requires_human_approval


def test_large_ledger_amount_requires_approval():
    result = validate_ledger_entry_amount(amount=100_000.0)
    assert not result.passed
    assert result.requires_human_approval


def test_small_ledger_amount_auto_approved():
    result = validate_ledger_entry_amount(amount=500.0)
    assert result.passed
    assert not result.requires_human_approval


def test_negative_ledger_amount_flagged():
    result = validate_ledger_entry_amount(amount=-50.0)
    assert not result.passed


def test_sensitive_actions_always_gated():
    assert requires_human_approval_for_action("e_way_bill")
    assert requires_human_approval_for_action("loan_application")
    assert requires_human_approval_for_action("gst_filing")
    assert not requires_human_approval_for_action("small_sale_entry")
