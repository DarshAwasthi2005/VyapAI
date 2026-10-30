import pytest

from app.enums import EntryType
from app.services.accounting import (
    build_purchase_transaction,
    build_sale_transaction,
    reconcile_input_tax_credit,
    verify_line_items_sum_to_total,
)


def test_sale_is_exactly_one_entry():
    """Simple books: one business event writes exactly ONE ledger row."""
    txn = build_sale_transaction(taxable_value=1000.0, gst_rate=18.0, party="Ramesh Kirana", source="voice_note")
    assert len(txn.lines) == 1
    line = txn.lines[0]
    assert line.account == "Sales"          # money in
    assert line.entry_type == EntryType.CREDIT
    assert line.amount == pytest.approx(1180.0)   # value + GST, what the merchant actually received
    assert line.gst_rate == 18.0            # tax kept on the row for reports


def test_sale_zero_gst_is_one_entry_at_face_value():
    txn = build_sale_transaction(taxable_value=500.0, gst_rate=0.0, party="Local buyer", source="manual")
    assert len(txn.lines) == 1
    assert txn.lines[0].amount == pytest.approx(500.0)


def test_purchase_is_exactly_one_entry():
    txn = build_purchase_transaction(taxable_value=2000.0, gst_rate=12.0, party="Wholesaler", source="bill_photo")
    assert len(txn.lines) == 1
    line = txn.lines[0]
    assert line.account == "Purchase"       # money out
    assert line.entry_type == EntryType.DEBIT
    assert line.amount == pytest.approx(2240.0)   # value + GST
    assert line.gst_rate == 12.0


def test_negative_taxable_value_rejected():
    with pytest.raises(ValueError):
        build_sale_transaction(taxable_value=-100.0, gst_rate=18.0, party="X", source="manual")


def test_reconcile_input_tax_credit_net_payable():
    result = reconcile_input_tax_credit(
        purchase_gst_amounts=[100.0, 50.0],
        output_gst_amounts=[300.0],
    )
    assert result["total_input_tax_credit"] == 150.0
    assert result["total_output_gst"] == 300.0
    assert result["net_gst_payable"] == 150.0
    assert result["carried_forward_credit"] == 0.0


def test_reconcile_input_tax_credit_carry_forward():
    result = reconcile_input_tax_credit(
        purchase_gst_amounts=[500.0],
        output_gst_amounts=[200.0],
    )
    assert result["net_gst_payable"] == 0.0
    assert result["carried_forward_credit"] == 300.0


def test_verify_line_items_sum_matches():
    items = [(5.0, 40.0, 200.0), (2.0, 100.0, 200.0)]
    ok, computed = verify_line_items_sum_to_total(items, stated_total=400.0)
    assert ok
    assert computed == 400.0


def test_verify_line_items_sum_mismatch_flagged():
    # 5 * 40 = 200, but line says amount=250 (handwriting error) and stated total agrees with the wrong figure
    items = [(5.0, 40.0, 250.0)]
    ok, computed = verify_line_items_sum_to_total(items, stated_total=250.0, tolerance=0.5)
    assert not ok  # computed (200) vs items-stated amount (250) mismatch
    assert computed == 200.0
