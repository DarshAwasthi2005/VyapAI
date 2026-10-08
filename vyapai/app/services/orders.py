"""
Parses free-text WhatsApp orders ("Send 5 bags of sugar and 2 cartons of
soap by 4 PM") into structured line items, then checks them against
inventory.

Two tiers, same as the OCR module:
  1. A deterministic regex parser that handles the common
     "<qty> <unit> of <item>" pattern in English/Hinglish, needs no API key,
     and is what tests/test_orders.py exercises.
  2. An LLM-assisted parser (via the vision/speech integrations' sibling —
     a text LLM call) for messier phrasing or full vernacular script. Wire
     this the same way as app/integrations/vision.py once you have a key.

The regex tier deliberately stays in this module (not integrations/) because
it has no external dependency and is core business logic, not an adapter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_QTY_UNIT_ITEM = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>bags?|cartons?|kg|kilograms?|box(?:es)?|packets?|pieces?|pcs?"
    r"|litres?|liters?|ltrs?|dozens?|bottles?|sacks?|quintals?|grams?|gms?|crates?|units?)"
    r"\s*(?:of\s*)?(?P<item>[a-zA-Z][a-zA-Z\s]*?)(?=(?:,|\band\b|\bby\b|$))",
    re.IGNORECASE,
)
_DEADLINE = re.compile(r"\bby\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)\b")


@dataclass
class ParsedLineItem:
    item_name: str
    quantity: float
    unit: str


@dataclass
class ParsedOrder:
    line_items: list[ParsedLineItem]
    deadline_text: str | None
    parse_confidence: str  # "high" (regex matched everything) | "low" (needs LLM/human review)


def parse_order_text(message: str) -> ParsedOrder:
    deadline_match = _DEADLINE.search(message)
    deadline = deadline_match.group(1) if deadline_match else None

    items: list[ParsedLineItem] = []
    for m in _QTY_UNIT_ITEM.finditer(message):
        item_name = m.group("item").strip().rstrip(".")
        if not item_name:
            continue
        items.append(ParsedLineItem(
            item_name=item_name,
            quantity=float(m.group("qty")),
            unit=m.group("unit").lower(),
        ))

    # Confidence heuristic: if we found zero items but the message clearly
    # has digits (likely an order), flag for LLM/human fallback rather than
    # silently doing nothing.
    confidence = "high" if items else ("low" if re.search(r"\d", message) else "high")
    return ParsedOrder(line_items=items, deadline_text=deadline, parse_confidence=confidence)


@dataclass
class InventoryCheckResult:
    item_name: str
    requested_qty: float
    available_qty: float
    in_stock: bool


def check_inventory(order: ParsedOrder, stock_levels: dict[str, float]) -> list[InventoryCheckResult]:
    """stock_levels: {item_name_lowercase: quantity_available}. Matching is
    exact-lowercase for this scaffold; production should use a fuzzy/aliased
    catalog match (e.g. "chini" -> "sugar")."""
    results = []
    for li in order.line_items:
        key = li.item_name.lower().strip()
        available = stock_levels.get(key, 0.0)
        results.append(InventoryCheckResult(
            item_name=li.item_name,
            requested_qty=li.quantity,
            available_qty=available,
            in_stock=available >= li.quantity,
        ))
    return results
