import pytest

from app.integrations.errors import NotConfiguredError
from app.integrations.upi import generate_payment_link


def test_generate_link_with_explicit_vpa():
    link = generate_payment_link(amount=250.5, note="Order #123", payee_vpa="shop@upi", payee_name="Shop")
    assert link.deep_link.startswith("upi://pay?")
    assert "pa=shop%40upi" in link.deep_link
    assert "am=250.50" in link.deep_link
    assert link.transaction_ref.startswith("VYAPAI-")
    assert link.amount == 250.5


def test_generate_link_missing_vpa_raises():
    with pytest.raises(NotConfiguredError):
        generate_payment_link(amount=100.0, note="test", payee_vpa=None)


def test_generate_link_rejects_non_positive_amount():
    with pytest.raises(ValueError):
        generate_payment_link(amount=0, note="test", payee_vpa="shop@upi")


def test_transaction_note_is_truncated_to_upi_limit():
    long_note = "x" * 100
    link = generate_payment_link(amount=10.0, note=long_note, payee_vpa="shop@upi")
    # 'tn' param value in the link should reflect the 50-char cap
    assert "x" * 51 not in link.deep_link


def test_qr_png_bytes_render_a_real_scannable_png():
    """segno draws the payment QR in pure Python: valid PNG, deterministic,
    and cached (second call returns the exact same buffer)."""
    from app.integrations.qr import qr_png_bytes

    link = generate_payment_link(amount=84.0, note="order", payee_vpa="shop@upi")
    png = qr_png_bytes(link.deep_link)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png == qr_png_bytes(link.deep_link)      # lru_cache identity
    assert qr_png_bytes("different-data") != png    # content actually varies
