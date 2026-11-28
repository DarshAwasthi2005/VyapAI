"""Render a normal written bill as a PNG image.

Buyers don't think in "markdown" — a bill is a piece of paper: the shop's
name on top, one written line per item, a big TOTAL at the bottom, and
(given a UPI link) the payment QR at the foot. This renders exactly that
as a PNG so it can be sent to the buyer's chat like a photo of a real bill.

Font policy: try system fonts known to carry the ₹ glyph (DejaVu on
Linux, Arial/Segoe on Windows); when none exists, fall back to Pillow's
bundled font and spell rupees out as "Rs." — a bill with tofu boxes is
worse than a bill that reads slightly differently.
"""
from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

_WIDTH = 640
_PAD = 36
_INK = (28, 28, 30)
_MUTED = (110, 110, 115)
_RULE = (205, 203, 196)
_PAPER = (252, 251, 248)

_RUPEE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux (VPS/CI)
    r"C:\Windows\Fonts\dejavusans.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]
# (chosen path|None, renders "₹"?) — resolved on first render and reused.
_glyph_probe: tuple[str | None, bool] | None = None

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int):
    global _glyph_probe
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    chosen: ImageFont.FreeTypeFont | ImageFont.ImageFont
    for path in _RUPEE_FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        if _glyph_probe is None:
            _glyph_probe = (path, _rupee_renderable(f))
        chosen = f
        _FONT_CACHE[size] = chosen
        return chosen
    try:
        chosen = ImageFont.load_default(size=size)  # Pillow >= 10
    except TypeError:
        chosen = ImageFont.load_default()
    if _glyph_probe is None:
        _glyph_probe = (None, False)
    _FONT_CACHE[size] = chosen
    return chosen


def _rupee_renderable(font) -> bool:
    probe = Image.new("L", (48, 48), 255)
    ImageDraw.Draw(probe).text((4, 4), "₹", font=font, fill=0)
    return probe.getbbox() is not None


def _munge(text: str, glyph_ok: bool) -> str:
    return text if glyph_ok else text.replace("₹", "Rs.")


def _line_height(glyph_ok: bool, size: int) -> int:
    return int(size * 1.5) + 3


def _draw(draw: ImageDraw.ImageDraw, xy: tuple[int, int], body: str, size: int,
          glyph_ok: bool, bold: bool = False, color=_INK,
          right_edge: int | None = None) -> None:
    f = _font(size + (2 if bold else 0))
    body = _munge(body, glyph_ok)
    if right_edge is not None:
        w = draw.textlength(body, font=f)
        draw.text((right_edge - w, xy[1]), body, font=f, fill=color)
    else:
        draw.text(xy, body, font=f, fill=color)


def render_bill_png(*, shop_name: str, bill_no: str, date_line: str,
                    items: list[dict],
                    subtotal_line: str | None = None,
                    discount_line: str | None = None,
                    total_line: str = "",
                    payment_line: str | None = None,
                    footer_lines: list[str] | None = None,
                    qr_png: bytes | None = None) -> bytes:
    """The written bill as PNG. Money strings arrive PRE-formatted —
    fulfillment owns money, this module only composes the page.

    items: [{"name": "sugar", "qty": "2", "unit": "kg",
             "price": 42.0,        # per-unit display, optional
             "amount": "₹84.00",   # right-aligned line total
           }, ...]
    """
    _font(12)  # resolve the font first so the ₹-probe below is decided once
    glyph = bool(_glyph_probe and _glyph_probe[1])
    footer_lines = footer_lines or []

    # --- first pass: wrap names into written rows -----------------------
    # One line per item, the way bills are written: "2 kg sugar @ ₹42.00"
    # on the left, the line total on the right, long names wrapped below.
    rows: list[dict] = []
    for it in items:
        wrapped = textwrap.wrap(it["name"], 34) or ["(item)"]
        meta = f"{it['qty']} {it['unit']}"
        if it.get("price"):
            meta += f"  @ ₹{it['price']:,.2f}"
        rows.append({"name": wrapped[0], "extras": wrapped[1:],
                     "meta": meta, "amount": it.get("amount") or ""})

    def lh(size: int) -> int:
        return _line_height(glyph, size)

    qr_side = 150 + 24 if qr_png is not None else 0
    def _item_h(r: dict) -> int:
        return lh(16) * (1 + len(r["extras"])) + lh(12)
    height = 2 * _PAD
    height += lh(26) + lh(13) + 22          # shop name + bill line + rule
    height += sum(_item_h(r) for r in rows)
    if subtotal_line or discount_line:
        height += 12 + (lh(14) if subtotal_line else 0) + (lh(14) if discount_line else 0)
    height += lh(20) + (lh(13) if payment_line else 0) + 18
    height += sum(lh(13) for _ in footer_lines)
    height += qr_side

    img = Image.new("RGB", (_WIDTH, height), _PAPER)
    draw = ImageDraw.Draw(img)
    right = _WIDTH - _PAD
    y = _PAD

    # --- header -----------------------------------------------------------
    _draw(draw, (_PAD, y), shop_name, 26, glyph, bold=True)
    y += lh(26)
    _draw(draw, (_PAD, y), " · ".join(x for x in (f"Bill {bill_no}", date_line) if x),
          13, glyph, color=_MUTED)
    y += lh(13) + 10
    draw.line(((_PAD, y), (right, y)), fill=_RULE, width=2)
    y += 12

    # --- items (one written line each) ------------------------------------
    for r in rows:
        _draw(draw, (_PAD, y), r["name"], 16, glyph)
        _draw(draw, (_PAD, y), r["amount"], 16, glyph, right_edge=right)
        y += lh(16)
        for extra in r["extras"]:
            _draw(draw, (_PAD, y), extra, 16, glyph)
            y += lh(16)
        _draw(draw, (_PAD, y), r["meta"], 12, glyph, color=_MUTED)
        y += lh(12)

    # --- totals ----------------------------------------------------------
    y += 6
    draw.line(((_PAD, y), (right, y)), fill=_RULE, width=1)
    y += 10
    if subtotal_line:
        _draw(draw, (_PAD, y), subtotal_line, 14, glyph, color=_MUTED, right_edge=right)
        y += lh(14)
    if discount_line:
        _draw(draw, (_PAD, y), discount_line, 14, glyph, color=_MUTED, right_edge=right)
        y += lh(14)
    _draw(draw, (_PAD, y), "TOTAL", 20, glyph, bold=True)
    _draw(draw, (_PAD, y), total_line, 20, glyph, bold=True, right_edge=right)
    y += lh(20)
    if payment_line:
        _draw(draw, (_PAD, y), payment_line, 13, glyph, color=_MUTED)
        y += lh(13)
    y += 8
    draw.line(((_PAD, y), (right, y)), fill=_RULE, width=1)
    y += 16

    # --- footer ----------------------------------------------------------
    for fl in footer_lines:
        _draw(draw, (_PAD, y), fl, 13, glyph, color=_MUTED)
        y += lh(13)

    # --- QR at the foot ----------------------------------------------------
    if qr_png is not None:
        y += 12
        try:
            qr_img = Image.open(io.BytesIO(qr_png)).convert("RGB")
            framed = Image.new("RGB", (qr_side, qr_side), "white")
            framed.paste(qr_img, (12, 12))
            img.paste(framed, ((_WIDTH - qr_side) // 2, y))
        except Exception:
            pass  # a bill without the QR beats a bill that never goes out

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
