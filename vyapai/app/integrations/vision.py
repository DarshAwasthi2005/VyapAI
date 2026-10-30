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


def extract_bill(image_bytes: bytes, media_type: str = "image/jpeg") -> BillExtraction:
    """Backend order (all free, see docs/ZERO_COST_BLUEPRINT.md):
    Gemini -> Groq -> Ollama. Gemini's free tier may use inputs to improve
    its products, so prefer Groq for real merchant bills."""
    if settings.gemini_api_key:
        raw = _call_gemini_vision(image_bytes, media_type)
    elif settings.groq_api_key:
        raw = _call_groq_vision(image_bytes, media_type)
    elif settings.ollama_base_url and settings.ollama_vision_model:
        raw = _call_ollama_vision(image_bytes)
    else:
        raise NotConfiguredError(
            "vision/OCR",
            ["GEMINI_API_KEY", "or GROQ_API_KEY", "or OLLAMA_BASE_URL + OLLAMA_VISION_MODEL"],
        )

    return _parse_extraction(raw)


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
    """POST to Google's Gemini API. See module docstring for free-tier setup."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    try:
        resp = httpx.post(
            url,
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": media_type,
                                          "data": base64.b64encode(image_bytes).decode()}},
                        {"text": EXTRACTION_PROMPT},
                    ]
                }],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
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
        raise UpstreamError(
            f"Gemini vision call failed ({e.response.status_code}): {e.response.text[:200]}"
        ) from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Gemini vision call failed: {e}") from e

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
            quantity=float(li["quantity"]),
            unit_price=float(li["unit_price"]),
            amount=float(li["amount"]),
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
