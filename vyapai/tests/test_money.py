"""Exact money math — integer paise, no float drift."""
import pytest

from app.utils.money import Money, format_inr, paise_to_rupees_decimal, rupees_to_paise


def test_rupees_to_paise_basic():
    assert rupees_to_paise(1) == 100
    assert rupees_to_paise("4.20") == 420
    assert rupees_to_paise(10.5) == 1050


def test_no_float_drift_over_many_additions():
    """0.1 added 1000 times as floats does not stay 100.0; paise do."""
    total = Money.from_rupees(0)
    penny = Money.from_rupees(0.1)  # exactly 10 paise, forever
    for _ in range(1000):
        total = total + penny
    assert total.paise == 10_000


def test_rounds_half_up():
    assert rupees_to_paise("1.005") == 101
    assert rupees_to_paise("1.004") == 100


def test_int_bypasses_float_question_mark():
    assert rupees_to_paise(7) == 700


def test_money_subtraction_can_go_negative():
    assert (Money(100) - Money(250)).paise == -150


def test_negative_money_flags():
    assert (Money(100) - Money(250)).is_negative()
    assert not Money(0).is_negative()
    assert Money(0).is_zero()


def test_division_exact_and_rounded():
    assert (Money(300) / 3).paise == 100
    # 5.01 / 2 = 2.505 -> rounds half-up to 251 paise
    assert (Money(501) / 2).paise == 251


def test_money_requires_int_paise():
    with pytest.raises(TypeError):
        Money(10.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]


def test_money_arithmetic_type_safety():
    with pytest.raises(TypeError):
        Money(100) + "1"  # type: ignore[operator]


def test_format_inr():
    assert format_inr(123456) == "₹1,234.56"
    assert format_inr(5) == "₹0.05"


def test_to_decimal_matches_paise():
    assert paise_to_rupees_decimal(420) == Money.from_rupees("4.20").to_decimal()
