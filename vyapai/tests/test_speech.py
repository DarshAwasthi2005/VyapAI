"""Speech-to-text adapter: backend preference, Groq Whisper call, fallback."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.integrations import speech
from app.integrations.errors import NotConfiguredError, UpstreamError


def test_backend_preference_groq_first_when_keyed():
    """The one REAL hosted backend (Groq Whisper) wins whenever its key is
    present — a configured-but-stubbed adapter must not kill the voice path."""
    with patch(groq_api_key="gsk_test", vosk_endpoint="http://127.0.0.1:14200"):
        assert speech._which_backends()[0] == "groq"
    # No Groq key: the vosk server is still tried.
    with patch(groq_api_key=None, vosk_endpoint="http://127.0.0.1:14200"):
        assert speech._which_backends() == ["vosk"]


def patch(**kw):
    from tests.conftest import patch_settings
    full = {k: None for k in ("groq_api_key", "bhashini_api_key",
                              "bhashini_endpoint", "indic_whisper_endpoint",
                              "vosk_endpoint")}
    full.update(kw)
    return patch_settings(**full)


def test_groq_transcription_calls_the_openai_compatible_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        captured.update(url=url, headers=headers or {}, data=data or {},
                        files=files or {})
        resp = SimpleNamespace(status_code=200)
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"text": "दो किलो चीनी और एक साबुन",
                             "language": "hi"}
        return resp

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    with patch(groq_api_key="gsk_test"):
        result = speech.transcribe(
            b"OGGBYTES", hint_language="hi")
    assert result.text == "दो किलो चीनी और एक साबुन"
    assert result.backend == "groq" and result.language == "hi"
    assert captured["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer gsk_test"
    assert captured["data"]["model"] == "whisper-large-v3-turbo"
    assert captured["data"]["language"] == "hi"
    name, content, mime = captured["files"]["file"]
    assert mime == "audio/ogg" and content == b"OGGBYTES"


def test_failed_preferred_backend_falls_through_to_vosk(monkeypatch):
    """Groq down / rate-limited must not kill the order — the chain tries the
    next configured backend (vosk here)."""
    def groq_boom(audio_bytes, hint_language=None):
        raise UpstreamError("Groq ASR call failed (429): rate limit")

    def fake_vosk(audio_bytes, hint_language=None):
        return speech.TranscriptionResult(
            text="do kilo cheeni", language="hi", confidence=0.7,
            backend="vosk")

    monkeypatch.setattr(speech, "_transcribe_groq", groq_boom)
    monkeypatch.setattr(speech, "_transcribe_vosk", fake_vosk)

    with patch(groq_api_key="gsk_test", vosk_endpoint="http://127.0.0.1:14200"):
        result = speech.transcribe(b"OGGBYTES")
    assert result.backend == "vosk" and result.text == "do kilo cheeni"


def test_no_backend_configured_raises_loudly():
    with patch():
        with pytest.raises(NotConfiguredError):
            speech.transcribe(b"OGGBYTES")
