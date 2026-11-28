"""
Handwritten bill OCR adapter, with two free backends — no paid API key needed
for either:

  1. Gemini API (hosted, default if configured). Google's free tier needs
     only a Google account, no credit card:
       - Get a key in ~3 minutes: https://aistudio.google.com/apikey
       - Set GEMINI_API_KEY in .env
     Of the free options this gives the best accuracy on messy handwriting
     and is the least code/setup, at the cost of sending bill photos to
     Google's API (free-tier traffic may be used to improve their models —
     see https://ai.google.dev/gemini-api/docs/pricing) and modest rate
     limits (tens of requests/minute, low thousands/day depending on model;
     limits change over time, so check the pricing page above if you hit one).

  2. Ollama (self-hosted, automatic fallback when GEMINI_API_KEY is unset).
     Runs a vision model on your own machine — zero signup, zero external
     network calls, unlimited use, but slower (30-120s/image on CPU) and
     less accurate on hard handwriting. One-time setup:
       ollama pull llama3.2-vision   # https://ollama.com/download

Both backends are prompted identically to return a strict JSON line-item
schema, so downstream guardrails (app/agents/guardrails.py) can validate
arithmetic deterministically rather than trusting the model's own totals.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

EXTRACTION_PROMPT = """You are reading a photo of a handwritten shop bill or ledger
entry from an Indian kirana/wholesale merchant. Extract the line items exactly as
written. Return ONLY valid JSON, no prose, matching this schema:

{
  "party_name": string or null,
  "date": string (as written) or null,
  "line_items": [
    {"description": string, "quantity": number, "unit_price": number, "amount": number}
  ],
  "stated_total": number or null,
  "notes": string or null
}

Do not compute or correct totals — transcribe exactly what is written, even if
the arithmetic looks wrong. Downstream validation handles that."""


@dataclass
class LineItem:
    description: str
    quantity: float
    unit_price: float
    amount: float


@dataclass
class BillExtraction:
    party_name: str | None
    date: str | None
    line_items: list[LineItem] = field(default_factory=list)
    stated_total: float | None = None
    notes: str | None = None
    raw_model_output: str = ""


def _vision_backends() -> list[str]:
    """Configured backends in preference order: Gemini, then a local Ollama
    server. Groq is NOT a vision backend anymore — its catalog dropped image
    models (verified against GET /openai/v1/models in Oct 2026), so adding
    it here would just burn a 404 on every photo. Callers try each in turn:
    one dead/half-configured backend must never stop a working one."""
    backends: list[str] = []
    if settings.gemini_api_key:
        backends.append("gemini")
    if settings.ollama_base_url and settings.ollama_vision_model:
        backends.append("ollama")
    return backends


def extract_bill(image_bytes: bytes, media_type: str = "image/jpeg") -> BillExtraction:
    """Try every configured backend in preference order until one succeeds:
    Gemini -> Ollama (all free; see docs/ZERO_COST_BLUEPRINT.md). Note: a
    hosted call sends the buyer's handwritten list to Google's API."""
    backends = _vision_backends()
    if not backends:
        raise NotConfiguredError(
            "vision/OCR",
            ["GROQ_API_KEY", "or GEMINI_API_KEY", "or OLLAMA_BASE_URL + OLLAMA_VISION_MODEL"],
        )

    last_error: UpstreamError | None = None
    for backend in backends:
        try:
            if backend == "groq":
                raw = _call_groq_vision(image_bytes, media_type)
            elif backend == "gemini":
                raw = _call_gemini_vision(image_bytes, media_type)
            else:
                raw = _call_ollama_vision(image_bytes)
            return _parse_extraction(raw)
        except UpstreamError as e:
            last_error = e
            continue
    assert last_error is not None  # at least one backend was attempted
    raise last_error


def _call_groq_vision(image_bytes: bytes, media_type: str) -> str:
    """POST to Groq's OpenAI-compatible chat endpoint.

    Groq's free tier (no card needed, key from https://console.groq.com/keys)
    handles vision via models like llama-4-scout. Unlike the Gemini free
    tier, Groq documents that inference inputs/outputs are not retained by
    default, which is why it is preferred for real merchant bills.
    """
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.groq_vision_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{media_type};base64,"
                                   f"{base64.b64encode(image_bytes).decode()}"
                        }},
                    ],
                }],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise UpstreamError(
                "Groq free-tier rate limit hit — wait and retry, or unset "
                "GROQ_API_KEY to fall back to the local Ollama backend."
            ) from e
        if e.response.status_code in (401, 403):
            raise UpstreamError(
                "Groq rejected the API key — check GROQ_API_KEY in .env "
                "(get a fresh one at https://console.groq.com/keys)."
            ) from e
        raise UpstreamError(
            f"Groq vision call failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Groq vision call failed: {e}") from e

    try:
        choice = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise UpstreamError(f"Unexpected Groq response shape: {resp.text[:200]}") from e
    return choice


def _call_gemini_vision(image_bytes: bytes, media_type: str) -> str:
    """POST to Google's Gemini API. See module docstring for free-tier setup.

    503 ("high demand") is a documented TRANSIENT — two short retries usually
    clear it, so a buyer's photo order doesn't bounce back for a_Google_
    capacity blip."""
    # Free-tier 503s ("high demand") hit ONE model variant at a time — a
    # sibling flash variant often has capacity when the configured one does
    # not, so walk a small ladder before giving up.
    model_ladder = [settings.gemini_model, "gemini-flash-latest"]
    seen = set()
    candidates = [m for m in model_ladder if not (m in seen or seen.add(m)) and m]
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": media_type,
                                  "data": base64.b64encode(image_bytes).decode()}},
                {"text": EXTRACTION_PROMPT},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }
    resp = None
    import time

    for model in candidates:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        for attempt in range(3):  # 3 attempts: 0s, +2s, +4s — for 503 spikes only
            try:
                # 120 s: free-tier capacity spikes queue requests for well over
                # a minute before answering; Render/class networks return fast
                # at the head but this client tolerates the slow tail too.
                resp = httpx.post(url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 503 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.response.status_code == 429:
                    raise UpstreamError(
                        "Gemini free-tier rate limit hit. Wait a minute and retry, unset "
                        "GEMINI_API_KEY to fall back to the local Ollama backend, or see "
                        "https://ai.google.dev/gemini-api/docs/pricing for current limits."
                    ) from e
                if e.response.status_code in (401, 403):
                    raise UpstreamError(
                        "Gemini rejected the API key — check GEMINI_API_KEY in .env "
                        "(get a fresh one at https://aistudio.google.com/apikey)."
                    ) from e
                if e.response.status_code == 503 and model is not candidates[-1]:
                    break  # jump to the next model in the ladder
                raise UpstreamError(
                    f"Gemini vision call failed ({e.response.status_code}): {e.response.text[:200]}"
                ) from e
            except httpx.HTTPError as e:
                raise UpstreamError(f"Gemini vision call failed: {e}") from e
        else:
            continue  # all attempts on this model exhausted → next model
        if resp is not None and resp.status_code == 200:
            break

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise UpstreamError(
            f"Unexpected Gemini response shape: {json.dumps(data)[:200]}"
        ) from e


def _call_ollama_vision(image_bytes: bytes) -> str:
    """POST to a local/self-hosted Ollama server — no key, no cloud call.

    `format: "json"` asks Ollama to constrain output to valid JSON. Vision
    models are slow on CPU-only machines — 60-120s for one bill photo is
    normal on a laptop; a GPU brings this down to a few seconds.
    """
    try:
        resp = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_vision_model,
                "prompt": EXTRACTION_PROMPT,
                "images": [base64.b64encode(image_bytes).decode()],
                "format": "json",
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.ConnectError as e:
        raise UpstreamError(
            f"Could not reach Ollama at {settings.ollama_base_url}. Is `ollama serve` "
            "running, and has the model been pulled (`ollama pull "
            f"{settings.ollama_vision_model}`)? Alternatively, set GEMINI_API_KEY "
            "for the hosted free-tier backend instead."
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Ollama vision call failed: {e}") from e

    return resp.json().get("response", "")


def _num(value) -> float:
    """Models legitimately return null for prices/quantities they can't read
    (an ORDER LIST has no prices at all) — treat as 0.0 instead of crashing
    the whole extraction."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_extraction(raw: str) -> BillExtraction:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpstreamError(
            f"Model did not return valid JSON: {e}. Raw output: {raw[:200]!r}"
        ) from e

    items = [
        LineItem(
            description=li["description"],
            quantity=_num(li.get("quantity")),
            unit_price=_num(li.get("unit_price")),
            amount=_num(li.get("amount")),
        )
        for li in data.get("line_items", [])
    ]
    return BillExtraction(
        party_name=data.get("party_name"),
        date=data.get("date"),
        line_items=items,
        stated_total=data.get("stated_total"),
        notes=data.get("notes"),
        raw_model_output=raw,
    )
