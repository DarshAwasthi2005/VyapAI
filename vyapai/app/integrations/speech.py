"""
Vernacular speech-to-text adapter.

Two backends supported, chosen by whichever is configured:
  - Bhashini (Government of India's multilingual API network)
  - A self-hosted Indic-Whisper endpoint

Swap in a real HTTP client (httpx) where marked — kept dependency-free here
so this module imports cleanly without network access during development.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.integrations.errors import NotConfiguredError, UpstreamError

SUPPORTED_LANGUAGES = {
    "hi": "Hindi", "ta": "Tamil", "gu": "Gujarati", "mr": "Marathi",
    "bn": "Bengali", "te": "Telugu", "kn": "Kannada", "pa": "Punjabi",
    "en": "English",
}


@dataclass
class TranscriptionResult:
    text: str
    language: str
    confidence: float
    backend: str


def _which_backends() -> list[str]:
    """Configured backends in preference order. A caller tries each in turn:
    if the preferred one is unreachable/stubbed, the next takes over instead
    of killing the voice path entirely (a half-configured environment must
    never stop a working one, e.g. a Vosk server up while Bhashini's HTTP
    client is still a stub)."""
    backends: list[str] = []
    if settings.groq_api_key:
        backends.append("groq")      # real hosted Whisper — works today
    if settings.bhashini_api_key and settings.bhashini_endpoint:
        backends.append("bhashini")
    if settings.indic_whisper_endpoint:
        backends.append("indic_whisper")
    if settings.vosk_endpoint:
        backends.append("vosk")
    return backends


def _which_backend() -> str:
    """The single preferred backend, for callers that want to know which
    one transcribed. Raises NotConfiguredError when nothing is configured."""
    backends = _which_backends()
    if not backends:
        raise NotConfiguredError(
            "speech",
            [
                "VOSK_ENDPOINT (self-hosted, free)",
                "or BHASHINI_API_KEY + BHASHINI_ENDPOINT (PoC-only free)",
                "or INDIC_WHISPER_ENDPOINT",
            ],
        )
    return backends[0]


def transcribe(audio_bytes: bytes, hint_language: str | None = None) -> TranscriptionResult:
    """Transcribe a short (<=10s) voice note, trying every configured
    backend in preference order until one succeeds.

    Raises NotConfiguredError if NO backend is set up; UpstreamError only if
    EVERY configured one failed. Callers (agents/graph_b2b.py, webhooks)
    catch both and fall back to asking the sender to type instead."""
    backends = _which_backends()
    if not backends:
        raise NotConfiguredError(
            "speech",
            [
                "VOSK_ENDPOINT (self-hosted, free)",
                "or BHASHINI_API_KEY + BHASHINI_ENDPOINT (PoC-only free)",
                "or INDIC_WHISPER_ENDPOINT",
            ],
        )

    last_error: UpstreamError | None = None
    for backend in backends:
        try:
            if backend == "groq":
                return _transcribe_groq(audio_bytes, hint_language)
            if backend == "bhashini":
                return _transcribe_bhashini(audio_bytes, hint_language)
            if backend == "vosk":
                return _transcribe_vosk(audio_bytes, hint_language)
            return _transcribe_indic_whisper(audio_bytes, hint_language)
        except UpstreamError as e:
            last_error = e
            continue
    assert last_error is not None  # at least one backend was attempted
    raise last_error


def _transcribe_vosk(audio_bytes: bytes, hint_language: str | None) -> TranscriptionResult:
    """POST to a self-hosted Vosk ASR server — completely free, no per-minute
    charge, runs offline on the same box (small Hindi model is 42 MB,
    Gujarati 100 MB, ~300 MB RAM at runtime; NOT available for Tamil).

    Contract: POST /asr with {"audio_b64": ..., "language": "hi"|"gu"|...}
    and reply {"text": ..., "language": ...}. Wrap Vosk with any tiny HTTP
    shim (e.g. FastAPI) on the host that also runs the API server — see
    docs/ZERO_COST_BLUEPRINT.md.
    """
    import base64

    import httpx

    try:
        resp = httpx.post(
            f"{settings.vosk_endpoint.rstrip('/')}/asr",
            json={
                "audio_b64": base64.b64encode(audio_bytes).decode(),
                "language": hint_language or "hi",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Vosk ASR call failed ({e.response.status_code}): {e}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(
            f"Could not reach Vosk ASR at {settings.vosk_endpoint}: {e}"
        ) from e
    data = resp.json()
    return TranscriptionResult(
        text=data.get("text", ""),
        language=data.get("language", hint_language or "hi"),
        confidence=float(data.get("confidence", 0.5)),
        backend="vosk",
    )


def _transcribe_groq(audio_bytes: bytes, hint_language: str | None) -> TranscriptionResult:
    """OpenAI-compatible hosted Whisper on Groq's free tier — the zero-setup
    path for WhatsApp/Telegram voice notes (ogg/opus goes straight up, no
    local model server needed). POST multipart to /audio/transcriptions:
    supports flac/mp3/mp4/mpeg/mpga/m4a/ogg/wav/webm up to 25 MB on the
    free tier; `language` (ISO-639-1) improves accuracy/latency for a
    vernacular order note."""
    import httpx

    if not settings.groq_api_key:
        raise NotConfiguredError("speech", ["GROQ_API_KEY"])
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            data={
                "model": settings.groq_asr_model,
                "response_format": "verbose_json",
                "language": hint_language or "hi",
            },
            files={"file": ("voice_note.ogg", audio_bytes, "audio/ogg")},
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise UpstreamError(f"Groq ASR call failed ({e.response.status_code}): {e}") from e
    except httpx.HTTPError as e:
        raise UpstreamError(f"Could not reach Groq ASR: {e}") from e
    data = resp.json()
    return TranscriptionResult(
        text=data.get("text", ""),
        language=data.get("language") or hint_language or "hi",
        confidence=float(data.get("confidence", 0.5)),
        backend="groq",
    )


def _transcribe_bhashini(audio_bytes: bytes, hint_language: str | None) -> TranscriptionResult:
    # TODO(deploy): replace with a real call, e.g.
    #   import httpx
    #   resp = httpx.post(
    #       f"{settings.bhashini_endpoint}/asr",
    #       headers={"Authorization": f"Bearer {settings.bhashini_api_key}"},
    #       json={"audio": base64.b64encode(audio_bytes).decode(), "sourceLanguage": hint_language or "hi"},
    #       timeout=15,
    #   )
    #   resp.raise_for_status()
    #   data = resp.json()
    #   return TranscriptionResult(data["text"], data["language"], data["confidence"], "bhashini")
    raise UpstreamError(
        "Bhashini adapter has valid credentials but the HTTP call is stubbed. "
        "Wire up the request in app/integrations/speech.py:_transcribe_bhashini."
    )


def _transcribe_indic_whisper(audio_bytes: bytes, hint_language: str | None) -> TranscriptionResult:
    # TODO(deploy): POST audio_bytes (multipart) to settings.indic_whisper_endpoint
    raise UpstreamError(
        "Indic-Whisper endpoint is configured but the HTTP call is stubbed. "
        "Wire up the request in app/integrations/speech.py:_transcribe_indic_whisper."
    )
