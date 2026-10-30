"""
Transcript -> structured bill/entry, completing the B2B voice path.

Before this module existed the voice graph transcribed into raw_text and then
... did nothing: `node_ingest` left a comment promising "a second LLM pass".
This is that pass. It uses the FREE hosted tiers (llm_text.complete), never
computes totals itself (that's still the guardrails' job), and expects the
same strict-JSON shape as vision.py's BillExtraction so both paths feed the
identical guardrail + ledger pipeline.

PII discipline (docs/ZERO_COST_BLUEPRINT.md §1c-5): customer/supplier phone
numbers are tokenized out before the text ever leaves the process — free-tier
LLM terms do not let us push raw personal data at them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.integrations import llm_text
from app.integrations.errors import NotConfiguredError, UpstreamError

_PHONE_RE = re.compile(r"(\+?\d[\d\s.-]{7,14}\d)")

STRUCTURE_PROMPT = """You are converting a spoken transcript from an Indian
kirana/wholesale merchant into a structured bookkeeping entry. Return ONLY
valid JSON, no prose, exactly this schema:

{
  "party_name": string or null,
  "transaction_kind": "sale" or "purchase",
  "line_items": [
    {"description": string, "quantity": number, "unit_price": number, "amount": number}
  ],
  "stated_total": number or null,
  "note": string or null
}

Rules:
- Transcribe amounts EXACTLY as spoken, even if the arithmetic looks wrong.
  Downstream validation (not you) checks the sums.
- If the transcript mixed two transactions, split them ONLY if unambiguous,
  otherwise pick the dominant one and say so in "note".
- Missing quantity/price/amount -> null for that field; never guess money.
"""


@dataclass
class PiiMask:
    """Rewrites phone numbers to stable tokens and restores them after."""

    original_to_token: dict[str, str] = field(default_factory=dict)

    def mask(self, text: str) -> tuple[str, PiiMask]:
        masked = text

        def _sub(m: re.Match) -> str:
            token = f"[PHONE_{len(self.original_to_token) + 1}]"
            self.original_to_token[m.group(0)] = token
            return token

        masked = _PHONE_RE.sub(_sub, masked)
        return masked, self

    def unmask(self, text: str) -> str:
        for original, token in self.original_to_token.items():
            text = text.replace(token, original)
        return text


@dataclass
class StructuredEntry:
    raw_transcript: str
    raw_model_output: str
    party_name: str | None = None
    transaction_kind: str = "sale"  # "sale" | "purchase"
    line_items: list[dict] = field(default_factory=list)
    stated_total: float | None = None
    note: str | None = None


def structure_transcript(transcript: str, *, hint_language: str | None = None) -> StructuredEntry:
    """Turn a spoken transcript into a structured entry.

    Raises NotConfiguredError when no LLM backend is configured (the graph
    turns that into a needs-human-approval flow, never a silent skip) and
    UpstreamError on API failure or malformed JSON.
    """
    if not transcript.strip():
        raise ValueError("transcript is empty — nothing to structure")

    masker = PiiMask()
    masked_text, masker = masker.mask(transcript)

    language_hint = f" (the transcript is mostly in language code {hint_language})" if hint_language else ""
    try:
        raw = llm_text.complete(
            f"Transcript{language_hint}:\n\"\"\"\n{masked_text}\n\"\"\"\n\n{STRUCTURE_PROMPT}",
            system="You transcribe and structure, you never calculate.",
            json_mode=True,
        )
        data = llm_text.parse_json_completion(raw)
    except (NotConfiguredError, UpstreamError):
        # Re-raise untouched: callers route these to human review; nothing
        # masked/unmasked happens on a failed call anyway.
        raise

    kind = data.get("transaction_kind") or "sale"
    if kind not in ("sale", "purchase"):
        kind = "sale"

    stated_total = data.get("stated_total")
    parsed = StructuredEntry(
        raw_transcript=transcript,
        raw_model_output=raw,
        party_name=data.get("party_name"),
        transaction_kind=kind,
        line_items=[
            {
                "description": li.get("description", ""),
                "quantity": li.get("quantity"),
                "unit_price": li.get("unit_price"),
                "amount": li.get("amount"),
            }
            for li in data.get("line_items", [])
            if isinstance(li, dict)
        ],
        stated_total=float(stated_total) if stated_total is not None else None,
        note=data.get("note"),
    )
    # Restore any phone tokens the model echoed back into the JSON fields.
    note = parsed.note
    if note:
        parsed.note = masker.unmask(note)
    party = parsed.party_name
    if party:
        parsed.party_name = masker.unmask(party)
    return parsed
