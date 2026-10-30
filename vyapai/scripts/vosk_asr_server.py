"""Local Vosk ASR server — the ₹0 speech-to-text backend for VyapAI.

Implements the exact contract app/integrations/speech.py expects:

    POST /asr   {"audio_b64": "<base64 bytes>", "language": "hi"}
             -> {"text": "...", "language": "hi", "confidence": 0.83}

Run it beside the API on the same machine (default port 14200), then set
VOSK_ENDPOINT=http://127.0.0.1:14200 in .env. Offline, free, no per-minute
cost; ~300 MB RAM with the small Hindi model.

Model choice: the small Hindi model (42 MB) understands Hindi speech, which
covers the pilot market; `language` is echoed back but this server hosts one
model. Point MODEL_DIR at vosk-model-small-gu-0.42 for Gujarati or download
a bigger model for better accuracy.

Usage:
    python scripts/vosk_asr_server.py            # 127.0.0.1:14200
    python scripts/vosk_asr_server.py --port 9000
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import wave

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:  # package import path when run as a module from the repo root
    from app.config import settings  # noqa: F401  (ensures .env is loaded)
except Exception:  # pragma: no cover - standalone script use
    pass

MODEL_DIR = os.environ.get("VOSK_MODEL_DIR", ".asr/vosk-model-small-hi-0.22")
SAMPLE_RATE = 16000

app = FastAPI(title="VyapAI Vosk ASR", version="0.1.0")
_model = None


class AsrRequest(BaseModel):
    audio_b64: str
    language: str | None = "hi"


def _get_model():
    """Load lazily: Vosk model load takes a few seconds and must not block
    app startup before the first request arrives."""
    global _model
    if _model is None:
        import vosk

        vosk.SetLogLevel(-1)
        _model = vosk.Model(MODEL_DIR)
    return _model


def _to_pcm16k(audio_bytes: bytes) -> bytes:
    """Normalise any supported container (wav / ogg-opus / mp3 / flac) to
    the raw 16 kHz mono s16le PCM Vosk's recognizer consumes."""
    # Fast path: already a plain WAV — read PCM directly, no decoder needed.
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getsampwidth() == 2 \
                    and wf.getframerate() == SAMPLE_RATE:
                return wf.readframes(wf.getnframes())
    except (wave.Error, EOFError):
        pass

    import numpy as np
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(audio_bytes), dtype="int16", always_2d=True)
    mono = data[:, 0]
    if rate != SAMPLE_RATE:
        # Linear resample — Vosk is trained at 16 kHz; other rates degrade
        # badly, so precise-enough interpolation is better than none.
        n_out = int(len(mono) * SAMPLE_RATE / rate)
        idx = np.linspace(0, len(mono) - 1, n_out)
        mono = np.interp(idx, np.arange(len(mono)), mono.astype(np.float32))
        mono = mono.astype(np.int16)
    return mono.tobytes()


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_DIR}


@app.post("/asr")
def transcribe(req: AsrRequest):
    try:
        audio = base64.b64decode(req.audio_b64)
    except Exception as e:
        raise HTTPException(422, f"audio_b64 is not valid base64: {e}") from e

    try:
        pcm = _to_pcm16k(audio)
    except Exception as e:
        raise HTTPException(422, f"Unsupported audio: {e}") from e

    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(_get_model(), SAMPLE_RATE)
    rec.SetWords(True)
    texts: list[str] = []
    confidences: list[float] = []

    # Feed in 4 kB chunks: Kaldi processes incrementally and a long note
    # never needs the whole buffer materialised inside the recognizer.
    chunk = 4096
    for i in range(0, len(pcm), chunk):
        piece = pcm[i:i + chunk]
        if rec.AcceptWaveform(piece):
            part = json.loads(rec.Result())
            if part.get("text"):
                texts.append(part["text"])
            confidences.extend(w.get("conf", 0.0) for w in part.get("result", []))

    final = json.loads(rec.FinalResult())
    if final.get("text"):
        texts.append(final["text"])
    confidences.extend(w.get("conf", 0.0) for w in final.get("result", []))

    text = " ".join(t.strip() for t in texts if t.strip()).strip()
    confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    return {"text": text, "language": req.language or "hi", "confidence": confidence}


def main() -> None:
    parser = argparse.ArgumentParser(description="VyapAI local Vosk ASR server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14200)
    args = parser.parse_args()

    # Warm the model so the first real voice note isn't slow (and a missing
    # model fails loudly now, not silently at 3 AM).
    print(f"loading Vosk model from {MODEL_DIR} ...", flush=True)
    _get_model()
    print(f"Vosk ASR ready on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
