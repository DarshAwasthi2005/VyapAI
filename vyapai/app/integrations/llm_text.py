"""
Text-completion adapter over the FREE hosted tiers, mirroring the same
backend discipline as vision.py: Gemini -> Groq -> Ollama, everything else
raises NotConfiguredError (never fabricates completion).

This is what was missing in the B2B voice path — a transcript alone is not a
structured entry. Preferred backend is Groq's free tier: no card, generous
limits, and documented no-default retention of inference inputs/outputs.
Gemini's free tier may use inputs to improve its products, so it is the
*last* hosted choice for anything sensitive.
"""
from __future__ import annotations

import json

import httpx

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError


def complete(prompt: str, *, system: str | None = None, json_mode: bool = False) -> str:
    """One-shot text completion. Returns the model's text output.

    Raises NotConfiguredError when no backend is configured and
    UpstreamError when a configured call fails — callers turn those into
    human-review flows, never silent fallbacks.
    """
    if settings.gemini_api_key:
        return _complete_gemini(prompt, system, json_mode)
    if settings.groq_api_key:
        return _complete_groq(prompt, system, json_mode)
    if settings.ollama_base_url:
        return _complete_ollama(prompt, system, json_mode)
    raise NotConfiguredError(
        "text LLM",
        ["GEMINI_API_KEY", "or GROQ_API_KEY", "or OLLAMA_BASE_URL"],
    )


# --- Groq (preferred) ----------------------------------------------------


def _complete_groq(prompt: str, system: str | None, json_mode: bool) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict = {
        "model": settings.groq_text_model,
        "messages": messages,
        "temperature": 0,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Groq text call failed ({e.response.status_code}): {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Groq text call failed: {e}") from e
    except (KeyError, IndexError) as e:
        raise UpstreamError(f"Unexpected Groq response shape: {e}") from e


# --- Gemini (hosted fallback; free tier trains on inputs — see module docs)


def _complete_gemini(prompt: str, system: str | None, json_mode: bool) -> str:
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    try:
        resp = httpx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_model}:generateContent",
            headers={"x-goog-api-key": settings.gemini_api_key},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Gemini text call failed ({e.response.status_code}): {e.response.text[:200]}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Gemini text call failed: {e}") from e
    except (KeyError, IndexError) as e:
        raise UpstreamError(f"Unexpected Gemini response shape: {e}") from e


# --- Ollama (self-hosted; no network) -------------------------------------


def _complete_ollama(prompt: str, system: str | None, json_mode: bool) -> str:
    body: dict = {
        "model": settings.ollama_vision_model,  # single local model for both
        "prompt": prompt,
        "stream": False,
    }
    if system:
        body["system"] = system
    if json_mode:
        body["format"] = "json"
    try:
        resp = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Ollama text call failed ({e.response.status_code}): {e}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Ollama text call failed: {e}") from e


def parse_json_completion(raw: str) -> dict:
    """Strict helper: a completion that promised JSON must BE JSON."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpstreamError(f"Model did not return valid JSON: {e}. Raw: {raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise UpstreamError(f"Model JSON was not an object: {raw[:200]!r}")
    return data
