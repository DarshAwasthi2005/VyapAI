"""
Money as integer paise.

Ledger correctness rule for this codebase: a rupee amount is an int count of
paise, never a bare float. Python floats can't represent 0.1 exactly, so a
long run of additions silently drifts off the books. Everything that
*computes* money going forward builds on the helpers here; DB columns are
Numeric(14,2).

The existing services (accounting.py etc.) still speak rupee-float at their
boundaries with round(..., 2) and are fully test-covered; migrating them is a
phased follow-up so this first change stays reviewable and green.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


def rupees_to_paise(rupees: float | Decimal | str | int) -> int:
    """Convert a rupee amount to integer paise, half-up on the third decimal.

    Accepts float (4.2), str ("4.2"), Decimal, or int (rupees).
    """
    if isinstance(rupees, int) and not isinstance(rupees, bool):
        return rupees * 100
    cents = (Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def paise_to_rupees_decimal(paise: int) -> Decimal:
    """Exact Decimal rupees for DB writes and JSON serialization."""
    return Decimal(paise) / Decimal(100)


def format_inr(paise: int) -> str:
    """₹1,234.56 — Indian-style grouping, always two decimals."""
    rupees = paise // 100
    p = paise % 100
    s = f"{rupees:,}"
    return f"₹{s}.{p:02d}"


@dataclass(frozen=True)
class Money:
    """Immutable paise-integer amount. All arithmetic stays exact."""

    paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.paise, int) or isinstance(self.paise, bool):
            raise TypeError("Money.paise must be an int count of paise")

    @classmethod
    def from_rupees(cls, rupees: float | Decimal | str | int) -> Money:
        return cls(rupees_to_paise(rupees))

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            raise TypeError("Money arithmetic requires Money")
        return Money(self.paise + other.paise)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            raise TypeError("Money arithmetic requires Money")
        return Money(self.paise - other.paise)

    def __mul__(self, factor: int) -> Money:
        if not isinstance(factor, int):
            raise TypeError("Money multiplication requires an integer factor")
        return Money(self.paise * factor)

    def __truediv__(self, divisor: int) -> Money:
        if not isinstance(divisor, int) or divisor == 0:
            raise TypeError("Money division requires a non-zero integer divisor")
        return Money(rupees_to_paise(self.paise / divisor / 100))

    def __neg__(self) -> Money:
        return Money(-self.paise)

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            raise TypeError("Money comparison requires Money")
        return self.paise < other.paise

    def __le__(self, other: Money) -> bool:
        return self == other or self < other

    def is_zero(self) -> bool:
        return self.paise == 0

    def is_negative(self) -> bool:
        return self.paise < 0

    def to_decimal(self) -> Decimal:
        return paise_to_rupees_decimal(self.paise)

    def formatted(self) -> str:
        return format_inr(self.paise)
