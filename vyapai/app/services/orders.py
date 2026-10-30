"""
Parses a free-text order message into ONE simple order entry — the first
"<qty> <unit> <item>" match is the order. Merchants enter one item at a
time; no multi-item splitting, no LLM tier.

A deterministic regex handles the common "<qty> <unit> of <item>" pattern in
English/Hinglish, needs no API key, and is what tests/test_orders.py
exercises. It deliberately stays in this module (not integrations/) because
it has no external dependency and is core business logic, not an adapter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- Vernacular normalisation -----------------------------------------------
# Vosk transcribes Hindi speech into Devanagari ("तीन किलो चीनी भेज दो") and
# people type Hinglish ("3 kilo chini"). The order regex below is English, so
# both scripts are mapped onto the same canonical words BEFORE parsing. This
# is a deterministic dictionary, not an LLM: same input always gives the same
# order, no key, no network, and unknown words simply stay unmatched (a
# low-confidence order instead of a hallucinated one).
_HI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_NUMERALS = {
    "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5", "पाँच": "5",
    "छह": "6", "छः": "6", "सात": "7", "आठ": "8", "नौ": "9", "दस": "10",
    "ग्यारह": "11", "बारह": "12", "पंद्रह": "15", "बीस": "20",
    "पच्चीस": "25", "तीस": "30", "चालीस": "40", "पचास": "50", "सौ": "100",
}

# Units as Vosk actually writes them, plus common Hinglish spellings.
_UNITS = {
    "किलो": "kg", "किलोग्राम": "kg", "किलोवाट": "kg", "kilo": "kg",
    "किलोस": "kg", "kilos": "kg", "किलोग्राम्स": "kg",
    "ग्राम": "grams", "ग्राम्स": "grams", "gram": "grams", "grams": "grams",
    "बैग": "bags", "बैग्स": "bags", "थैला": "bags", "थैले": "bags",
    "बोरी": "sacks", "बोरियां": "sacks", "बोरियाँ": "sacks",
    "डिब्बा": "boxes", "डिब्बे": "boxes", "dabba": "boxes", "dibba": "boxes",
    "पैकेट": "packets", "पैकेट्स": "packets",
    "नग": "pieces", "पीस": "pieces", "पीसी": "pieces",
    "लीटर": "litres", "लीटर्स": "litres", "litre": "litres",
    "दर्जन": "dozens", "दर्ज़न": "dozens", "darjan": "dozens",
    "कार्टन": "cartons", "कार्टून": "cartons", "carton": "cartons",
    "बोतल": "bottles", "बोतलें": "bottles",
    "क्विंटल": "quintals", "कट्टा": "sacks",
}

# Catalog aliases: the everyday Hindi/Hinglish name of an item -> the name the
# shop's catalog uses (order_intake's unit_prices/stock_levels keys).
_ITEMS = {
    "चीनी": "sugar", "शक्कर": "sugar", "chini": "sugar", "cheeni": "sugar",
    "साबुन": "soap", "sabun": "soap",
    "चावल": "rice", "chawal": "rice", "chaawal": "rice",
    "आटा": "flour", "aata": "flour", "atta": "flour",
    "नमक": "salt", "namak": "salt",
    "तेल": "oil", "tel": "oil",
    "दाल": "dal", "daal": "dal",
    "चाय": "tea", "chai": "tea",
    "मसाला": "masala", "masala": "masala",
}

# Politeness/filler verbs that follow an item in speech — dropped so they
# never leak into the item name.
_FILLERS = {"भेजो", "भेज", "दो", "दीजिए", "दीजिये", "चाहिए", "चाहिये", "please",
            "bhejo", "bhej", "chahiye", "chahiya", "dedo"}

# Hinglish numerals typed in Latin script. "do" is deliberately absent from
# the plain dictionary: it means "two" before a unit ("do kilo") but "give"
# elsewhere ("chini bhej do"), so it is resolved with that context below.
_LATIN_NUMERALS = {
    "ek": "1", "teen": "3", "char": "4", "chaar": "4", "paanch": "5",
    "panch": "5", "chhah": "6", "chah": "6", "saat": "7", "aath": "8",
    "nau": "9", "das": "10", "bees": "20", "pachas": "50",
}


def normalize_vernacular(message: str) -> str:
    """Map Devanagari numerals/units/items and Hinglish aliases onto the
    canonical English words the order regex understands. Latin text that is
    already canonical passes through unchanged."""
    text = message.translate(_HI_DIGITS)
    # Deadline phrases: "4 बजे तक" / "4 बजे" mean "by 4" to the English
    # _DEADLINE regex ("by <time>"); translate before word mapping so the
    # number is already in Latin digits.
    text = re.sub(r"(\d{1,2}(?::\d{2})?)\s*बजे(?:\s*तक)?", r"by \1", text)

    out: list[str] = []
    words = re.split(r"(\s+)", text)
    for idx, word in enumerate(words):
        if not word.strip():
            out.append(word)
            continue
        bare = word.strip(".,;:!?()[]\"'").lower()
        if bare == "do":
            # "do kilo" / "do bags" = 2 of that unit; otherwise "bhej do".
            nxt = next((w for w in words[idx + 1:] if w.strip()), "")
            nxt_bare = nxt.strip(".,;:!?()[]\"'").lower()
            out.append("2" if nxt_bare in _UNITS else " ")
            continue
        if bare in _FILLERS:
            out.append(" ")
            continue
        mapped = (_NUMERALS.get(bare) or _LATIN_NUMERALS.get(bare)
                  or _UNITS.get(bare) or _ITEMS.get(bare))
        if mapped:
            # Keep surrounding punctuation/digits intact.
            out.append(word.replace(bare, mapped, 1) if bare in word.lower()
                       else mapped)
        else:
            out.append(word)
    return "".join(out)


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
    parse_confidence: str  # "high" (order parsed) | "low" (needs human review)


def parse_order_text(message: str) -> ParsedOrder:
    message = normalize_vernacular(message)
    deadline_match = _DEADLINE.search(message)
    deadline = deadline_match.group(1) if deadline_match else None

    items: list[ParsedLineItem] = []
    # One entry at a time: only the FIRST "<qty> <unit> <item>" match is the
    # order. Anything after "and" is ignored — send it as a second order.
    m = _QTY_UNIT_ITEM.search(message)
    if m:
        item_name = m.group("item").strip().rstrip(".")
        if item_name:
            items.append(ParsedLineItem(
                item_name=item_name,
                quantity=float(m.group("qty")),
                unit=m.group("unit").lower(),
            ))

    # Confidence heuristic: if we found no item but the message clearly has
    # digits (likely an order), flag for human review rather than silently
    # doing nothing.
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
