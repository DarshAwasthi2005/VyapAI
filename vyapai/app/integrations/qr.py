"""
QR code rendering for payment links.

segno renders PNG natively in pure Python — no Pillow, no native deps, no
network call: a scannable "Pay ₹X" QR costs nothing to produce and can be
sent to the buyer over any messaging channel as an image.
"""
from __future__ import annotations

import io
from functools import lru_cache

import segno


@lru_cache(maxsize=64)
def qr_png_bytes(data: str) -> bytes:
    """PNG bytes encoding `data` as a QR code.

    Cached: the same UPI deep link is re-rendered for retries/reminders
    without repaying the render cost. scale=8/border=2 keeps the file small
    (~a few KB) while staying comfortably scannable on a phone camera.
    """
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()
