"""Voice-path text structuring: the transcript → structure LLM pass.

The LLM call is mocked; what's under test is the contract — strict JSON,
PII masked before the call and rehydrated after, and money never guessed."""
import pytest

from app.integrations.errors import NotConfiguredError, UpstreamError
from app.services import text_structuring
from app.services.text_structuring import PiiMask, structure_transcript
from tests.conftest import patch_settings

_GOOD = {
    "party_name": "Sharma Traders",
    "transaction_kind": "sale",
    "line_items": [
        {"description": "sugar", "quantity": 5, "unit_price": 42, "amount": 210},
    ],
    "stated_total": 210,
    "note": "buyer on [PHONE_1]",
}


def _patch_llm(monkeypatch, raw):
    monkeypatch.setattr(text_structuring.llm_text, "complete", lambda *a, **kw: raw)


def test_clean_transcript_structures_cleanly(monkeypatch):
    seen = {}

    def fake_complete(prompt, system=None, json_mode=False):
        seen["prompt"] = prompt
        return str(_GOOD).replace("'", '"')

    monkeypatch.setattr(text_structuring.llm_text, "complete", fake_complete)
    out = structure_transcript("Sharma traders se 5 bag cheeni 210 rupaye")

    assert out.party_name == "Sharma Traders"
    assert out.transaction_kind == "sale"
    assert out.line_items[0]["amount"] == 210
    assert out.stated_total == 210
    # a system prompt travels with the call and JSON mode is requested
    # money fields reach the prompt exactly as spoken — no recalculation here
    assert "Transcript" in seen["prompt"]


def test_phone_numbers_masked_before_call_and_rehydrated_after(monkeypatch):
    calls = []

    def fake_complete(prompt, system=None, json_mode=False):
        calls.append(prompt)
        reply = dict(_GOOD)
        reply["note"] = "Shot on [PHONE_1], call him"
        return str(reply).replace("'", '"')

    monkeypatch.setattr(text_structuring.llm_text, "complete", fake_complete)
    out = structure_transcript("5 bags sugar to 9876543210 by 4pm")

    # The number never reached the LLM prompt as a raw digit string.
    assert "9876543210" not in calls[0]
    assert "[PHONE_" in calls[0]
    # …and was restored in the notes that come back to the merchant.
    assert out.note is not None and "9876543210" in out.note


def test_bad_json_raises_upstream_not_silently_accepted(monkeypatch):
    _patch_llm(monkeypatch, "lorem ipsum, not json at all")
    with pytest.raises(UpstreamError):
        structure_transcript("some transcript")


def test_not_configured_propagates():
    # Force every LLM backend off (ollama_base_url defaults to localhost and
    # would otherwise attempt a real network call). The NotConfiguredError
    # must reach the graph so it routes to human review instead of guessing.
    with patch_settings(gemini_api_key="", groq_api_key="", ollama_base_url=""):
        with pytest.raises(NotConfiguredError):
            structure_transcript("hello")


def test_empty_transcript_rejected_before_any_llm_call(monkeypatch):
    called = {"n": 0}

    def fake_complete(*a, **kw):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(text_structuring.llm_text, "complete", fake_complete)
    with pytest.raises(ValueError):
        structure_transcript("   ")
    assert called["n"] == 0


def test_transaction_kind_coerced_to_sale_on_garbage(monkeypatch):
    reply = dict(_GOOD)
    reply["transaction_kind"] = "donation"
    _patch_llm(monkeypatch, str(reply).replace("'", '"'))
    out = structure_transcript("hello ledger")
    assert out.transaction_kind == "sale"


def test_pii_mask_roundtrip():
    m = PiiMask()
    masked, m = m.mask("ring 9876543210 or +91 98765 43210")
    assert "9876543210" not in masked
    assert "[PHONE_1]" in masked and "[PHONE_2]" in masked
    assert m.unmask(masked) == "ring 9876543210 or +91 98765 43210"
