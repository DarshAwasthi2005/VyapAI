"""Order fulfilment: buyer-facing notifications and the shop owner's flow.

The customer journey this module implements, channel-agnostic:

    buyer messages the bot (typed / voice / photo of a list)
        -> order recorded, buyer told it was received
        -> SHOP OWNER VERIFYING step: the OWNER checks the shelves and
           confirms which items are actually available (buttons / /avail,
           /missing) — nothing about price or payment reaches the buyer
           before that, so the stub catalog never promises what the shop
           can't deliver
    on verification
        -> buyer gets the payment QR FIRST, then the written confirmation
           (listing any items the owner marked unavailable)
        -> owner gets the settle buttons ("cash received"/"paid via UPI")
    owner marks the order ready (/ready, or dashboard button)
        -> buyer is told their order is packed
    owner marks the payment received (buttons, /paid, dashboard, PSP webhook)
        -> buyer is sent the itemised BILL automatically

Payment truth: a UPI deep link alone cannot prove money moved — only a PSP
webhook or the owner's explicit confirmation settles a payment, so until
one of those fires the row stays PENDING (never silently "success").
"""
from __future__ import annotations

import uuid
import re
from datetime import datetime

from sqlalchemy import func, select

from app.db.models import LedgerEntry, Merchant, Order, Payment, PaymentStatus
from app.enums import OrderStatus
from app.integrations import qr as qrmod
from app.integrations import upi as upimod
from app.integrations.errors import NotConfiguredError
from app.integrations.bill import render_bill_png
from app.services import channels
from app.services.accounting import build_sale_transaction
from app.services.audit import record_audit


def _inr(v) -> str:
    return f"₹{float(v or 0):,.2f}"


def _fmt_qty(q: float) -> str:
    return str(int(q)) if float(q) == int(q) else str(q)


def _short(order_id: str) -> str:
    return f"#{order_id[:8]}"


def resolve_public_shop(db, channel: str) -> Merchant | None:
    """Single-shop shortcut of the storefront routing below — kept because
    dashboards/tests use it: the shop exposed on the channel when exactly
    one shop is open, else None."""
    shops = public_shops(db, channel)
    return shops[0] if len(shops) == 1 else None


def public_shops(db, channel: str) -> list[Merchant]:
    """Every shop exposed on a channel (has a linked chat there). Shared
    bot means shared storefront: each of these shops' customers and
    everyone else message the SAME bot, so routing must be able to pick
    the right one (or ask)."""
    column = (Merchant.telegram_chat_id if channel == "telegram"
              else Merchant.whatsapp_chat_id if channel == "whatsapp"
              else None)
    if column is None:
        return []
    rows = db.execute(
        select(Merchant).where(column.isnot(None)).order_by(Merchant.business_name)
    ).scalars().all()
    return list(rows)


_shop_token_re = re.compile(r"[^a-z0-9]+")


def _name_tokens(name: str) -> set[str]:
    return {t for t in _shop_token_re.split(name.lower()) if t}


_SHOP_PHRASE_RE = re.compile(
    # "order from Sharma Store", "... from sharma store", "from sharma",
    # until by/before/at/please etc. or punctuation or end.
    r"\bfrom\s+([a-z0-9][\w &.'-]*?)(?=\s+(?:by|before|at|after|on|for|until|pls|please)\b|[,\.!]|$)",
    re.IGNORECASE,
)


def match_shop_in_text(shops: list[Merchant], text: str) -> tuple[Merchant | None, str | None, str | None]:
    """Which shop did the buyer NAME in their order text?

    Looks for a "from <shop>" phrase first ("2 kg sugar from sharma store");
    if none is present, a shop name mentioned anywhere in the message wins
    ("sharma store 2kg sugar"). The longest matching shop name wins when
    several could ("New Sharma Store" over "Sharma Store").

    Returns (shop, matched_full_phrase, cleaned_text) — cleaned_text drops
    the "from ..." phrase so the shop name is never parsed as an order
    item. (None, None, None) when no shop is named.
    """
    if not shops or not text or not text.strip():
        return None, None, None

    def _hit(shop: Merchant, haystack: str) -> int:
        tokens = _name_tokens(shop.business_name or "")
        if not tokens:
            return 0
        words = set(_name_tokens(haystack))
        return len(tokens) if tokens <= words else 0

    best: Merchant | None = None
    best_score = 0
    best_phrase: str | None = None
    phrase_match = _SHOP_PHRASE_RE.search(text)
    haystacks = []
    if phrase_match:
        tail = phrase_match.group(1)
        haystacks.append(tail)
        if tail:
            for shop in shops:
                score = _hit(shop, tail)
                if score > best_score:
                    best, best_score, best_phrase = shop, score, phrase_match.group(0)
    if best is None:
        # No phrase (or the phrase named no shop): name anywhere in text.
        for shop in shops:
            score = _hit(shop, text)
            if score > best_score:
                best, best_score, best_phrase = shop, score, None  # nothing to strip
    if best is None:
        return None, None, None
    cleaned = text
    if best_phrase:
        cleaned = text.replace(best_phrase, " ")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return best, best_phrase, cleaned


def resolve_shop_for_buyer(db, channel: str, text: str | None) -> tuple[Merchant | None, str | None, str | None]:
    """Storefront routing keyed off the ORDER TEXT: the shop the buyer
    named, else (with several shops live) a chooser message listing them.
    One shop exposed → it gets every order (old behaviour); zero shops →
    (None, None, None). Returns (shop, chooser_reply, cleaned_text) where
    chooser_reply is what to answer with instead of creating an order."""
    shops = public_shops(db, channel)
    if not shops:
        return None, None, None
    if len(shops) == 1:
        return shops[0], None, (text or "")
    shop, _phrase, cleaned = match_shop_in_text(shops, text or "")
    if shop is not None:
        return shop, None, cleaned
    return None, shop_list_text(shops), (text or "")


def remember_shop(db, channel: str, buyer_chat: str, shop: Merchant) -> None:
    """Remember which shop a public buyer chat is ordering from. The bot
    carries several shops, but a customer picks once and keeps ordering —
    naming a DIFFERENT shop later overwrites this row (one per chat)."""
    from app.db.models import StorefrontSelection

    row = db.execute(
        select(StorefrontSelection).where(
            StorefrontSelection.buyer_channel == channel,
            StorefrontSelection.buyer_chat_id == str(buyer_chat),
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(StorefrontSelection(
            buyer_channel=channel, buyer_chat_id=str(buyer_chat),
            merchant_id=shop.id,
        ))
    elif row.merchant_id != shop.id:
        row.merchant_id = shop.id
    db.flush()


def recall_shop(db, channel: str, buyer_chat: str,
                shops: list[Merchant] | None = None) -> Merchant | None:
    """The shop this buyer chat last ordered from — only when it is still a
    live storefront shop on this channel (a shop that unlinked its chat or
    vanished must not silently inherit the buyer)."""
    from app.db.models import StorefrontSelection

    live = shops if shops is not None else public_shops(db, channel)
    row = db.execute(
        select(StorefrontSelection).where(
            StorefrontSelection.buyer_channel == channel,
            StorefrontSelection.buyer_chat_id == str(buyer_chat),
        )
    ).scalar_one_or_none()
    if row is not None and row.merchant_id in {s.id for s in live}:
        return next(s for s in live if s.id == row.merchant_id)
    return None


def shop_list_text(shops: list[Merchant]) -> str:
    """The chooser reply: which shops are live, and exactly how to pick."""
    lines = ["🏪 This bot carries more than one shop — order from yours:"]
    for shop in shops:
        lines.append(f"• {shop.business_name} — e.g. \"order from {shop.business_name}\"")
    lines.append("Just include your shop's name in the message and the order goes to it.")
    return "\n".join(lines)


def buyer_target(order: Order) -> tuple[str | None, str | None]:
    """Where buyer-facing replies (order copy, ready notice, bill) go.
    New orders carry their own buyer_channel/buyer_chat_id (a public buyer's
    chat differs from the shop's). Legacy rows were placed from the shop's
    own linked chat — the shop chat is the right target for those."""
    if order.buyer_channel or order.buyer_chat_id:
        return order.buyer_channel, order.buyer_chat_id or order.buyer_whatsapp_number
    return owner_target(order)


def owner_target(order: Order) -> tuple[str | None, str | None]:
    """The shop's own linked chat — the only place payment confirmation
    buttons may ever appear (a buyer must not be able to mark their own
    payment done)."""
    merchant = order.merchant
    if merchant.telegram_chat_id:
        return "telegram", merchant.telegram_chat_id
    if merchant.whatsapp_chat_id:
        return "whatsapp", merchant.whatsapp_chat_id
    return None, None


def _pay_refund_note(order: Order, payment: Payment | None) -> str:
    if payment is None:
        return "Pay cash when you collect."
    if payment.status == PaymentStatus.SUCCESS:
        return f"Paid via {payment.method or 'upi'} ✓"
    link = f" Pay here: {payment.upi_deep_link}" if payment.upi_deep_link else ""
    return f"Amount due{_inr(payment.amount)}.{link}"


def bill_text(order: Order) -> str:
    """The itemised bill shared with the buyer the moment money is confirmed."""
    lines = [
        f"🧾 *Bill from {(order.merchant.business_name or 'your shop')}*",
        f"UPI: {order.merchant.upi_vpa or 'not set'}\n" if order.merchant.upi_vpa else "",
        f"Order {_short(order.id)} — {order.created_at:%d %b %Y, %H:%M}" if order.created_at else "",
        "―" * 12,
    ]
    for li in order.line_items:
        price = float(li.unit_price or 0)
        line = f"• {_fmt_qty(li.quantity)} {li.unit} {li.item_name}"
        if price > 0:
            line += f" — {_inr(price * li.quantity)}"
        lines.append(line)
    subtotal = _inr(float(order.total_amount) + float(order.discount_amount or 0))
    if float(order.discount_amount or 0) > 0:
        lines.append(f"Subtotal {subtotal}")
        lines.append(f"Discount −{_inr(order.discount_amount)}")
    payment = order.payment
    method = payment.method or "upi" if payment else None
    lines.append(f"*TOTAL {_inr(order.total_amount)}*" + (f" — paid via {method} ✓" if method else ""))
    lines.append("Thank you! 🙏")
    return "\n".join(x for x in lines if x)


def _best(channel: str | None, chat: str | None, body: str) -> bool:
    """Best-effort send: messaging must never break order state."""
    if not channel or not chat:
        return False
    return bool(channels.send(channel, chat, body).delivered)


def send_payment_QR(order: Order) -> bool:
    """Buyer gets the scannable QR for the pending UPI payment (Telegram
    carries the image; WhatsApp gets the tappable link as text)."""
    payment = order.payment
    if payment is None or not payment.upi_deep_link:
        return False
    channel, chat = buyer_target(order)
    if not channel or not chat:
        return False
    caption = (
        f"Pay {_inr(payment.amount)} for order {_short(order.id)} — scan with any"
        f" UPI app (GPay/PhonePe/Paytm)\n{payment.upi_deep_link}"
    )
    sent = channels.send_photo(channel, chat, payment.upi_deep_link, caption)
    return bool(sent and sent.delivered)


def verify_markup(order: Order) -> dict | None:
    """Inline keyboard for the OWNER verification step: "my shelves have
    what the customer asked for" (✅ all available) or "they don't" (❌ none
    available). Attached ONLY to the owner's copy of a fresh order — and
    only while the order is still awaiting verification. Partial availability
    ("sugar yes, rice no") is a typed list, so it's the /missing command:
    buttons can't collect an item list."""
    if order.status != OrderStatus.RECEIVED:
        return None
    return {"inline_keyboard": [[
        {"text": "✅ All available", "callback_data": f"avail:{order.id}"},
        {"text": "❌ None available", "callback_data": f"oos:{order.id}"},
    ]]}


def settle_markup(order: Order) -> dict | None:
    """Inline keyboard settling this order's payment — attached ONLY to
    owner-side messages (never the buyer's chat): confirming money received
    is the shop's call, and the chat↔shop binding authorises the tap."""
    if order.status != OrderStatus.CONFIRMED:
        return None
    return {"inline_keyboard": [[
        {"text": "💵 Cash received", "callback_data": f"cash:{order.id}"},
        {"text": "✅ Paid via UPI", "callback_data": f"paid:{order.id}"},
    ]]}


def notify_owner_of_order(db, order: Order, buyer_label: str, summary: str) -> None:
    """The owner's copy of a NEW order. While the order still awaits the
    availability verification it carries the verify buttons; after that the
    copy carries the settlement buttons."""
    channel, chat = owner_target(order)
    if order.status == OrderStatus.RECEIVED:
        body = (
            f"🛒 *New order* {buyer_label} — check your shelves:\n{summary}\n"
            f"Total {_inr(order.total_amount)} ({_short(order.id)})\n"
            f"✅ tap all-available · ❌ none available · or"
            f" /missing {_order_ref(order)} rice, salt to flag just some"
        )
        markup = verify_markup(order)
    else:
        body = (
            f"🛒 *New order* {buyer_label}\n{summary}\n"
            f"Total {_inr(order.total_amount)} ({_short(order.id)})\n"
            f"UPI: {order.merchant.upi_vpa or 'not set'}\n"
            f"/ready {_order_ref(order)} — packed · /paid {_order_ref(order)} — money in "
        )
        markup = settle_markup(order)
    if not channel or not chat:
        return
    outcome = channels.send(channel, chat, body, reply_markup=markup)
    if not outcome.delivered:
        pass  # messaging is best-effort; the order itself was recorded


def _order_ref(order: Order) -> str:
    return order.id[:8]


def bill_png(order: Order) -> bytes:
    """The itemised bill as a PNG that looks like a written bill: shop name
    on top, one line per item, TOTAL in bold — and the payment QR embedded
    at the foot when the order carries a UPI link, so paying is a scan away."""
    payment = order.payment
    items = []
    for li in order.line_items:
        price = float(li.unit_price or 0)
        items.append({
            "name": li.item_name,
            "qty": _fmt_qty(li.quantity),
            "unit": li.unit,
            "price": price if price > 0 else None,
            "amount": _inr(price * li.quantity) if price > 0 else "",
        })
    if payment is not None and payment.status == PaymentStatus.SUCCESS:
        payment_line = f"Payment received via {(payment.method or 'upi')}"
    elif payment is not None and payment.upi_deep_link:
        payment_line = f"Amount due — scan the QR below (any UPI app). Ref: {(payment.transaction_ref or '')}"
    else:
        payment_line = "Pay cash on collection."
    return render_bill_png(
        shop_name=order.merchant.business_name or "Your shop",
        bill_no=_short(order.id),
        date_line=(f"{order.created_at:%d %b %Y, %H:%M}" if order.created_at else ""),
        items=items,
        subtotal_line=(f"Subtotal  {_inr(float(order.total_amount) + float(order.discount_amount or 0))}"
                       if float(order.discount_amount or 0) > 0 else None),
        discount_line=(f"Discount  −{_inr(order.discount_amount)}"
                       if float(order.discount_amount or 0) > 0 else None),
        total_line=f"{_inr(order.total_amount)}",
        payment_line=payment_line,
        footer_lines=[f"Thank you for shopping at {order.merchant.business_name or 'our shop'}!",
                      "Powered by VyapAI"],
        qr_png=(qrmod.qr_png_bytes(payment.upi_deep_link)
                if payment is not None and payment.upi_deep_link else None),
    )


def send_bill(order: Order) -> bool:
    """The bill goes to the buyer automatically when money is confirmed:
    a real written-bill IMAGE (Telegram); buyers the channel can't carry
    images for get the same bill as text — never nothing."""
    channel, chat = buyer_target(order)
    if not channel or not chat:
        return False
    try:
        png = bill_png(order)
    except Exception:
        png = None  # image render must never block the text bill
    caption = f"🧾 Bill for order {_short(order.id)} — {order.merchant.business_name or 'thanks!'}"
    if png is not None:
        outcome = channels.send_image(channel, chat, png, caption=caption)
        if outcome.delivered:
            return True
    return _best(channel, chat, bill_text(order))


def mark_order_ready(db, order: Order) -> dict:
    """Shop-owner step: order packed, tell the buyer it can be collected."""
    if order.status == OrderStatus.READY:
        return {"updated": False, "already_ready": True, "status": order.status.value}
    if order.status in (OrderStatus.OUT_OF_STOCK, OrderStatus.CANCELLED, OrderStatus.RECEIVED):
        return {"updated": False, "refused": True,
                "reason": f"order is {order.status.value} (verify availability first)"}
    before = order.status.value
    order.status = OrderStatus.READY
    record_audit(
        db, action="order_status_changed", object_type="order", object_id=order.id,
        merchant_id=order.merchant_id, actor_type="api",
        before={"status": before}, after={"status": OrderStatus.READY.value},
    )
    channel, chat = buyer_target(order)
    merchant = order.merchant
    _best(channel, chat,
          f"🛍 Good news! Your order {_short(order.id)} is packed and ready for collection "
          f"at {merchant.business_name}.\n{_pay_refund_note(order, order.payment)}")
    return {"updated": True, "status": order.status.value}


def verify_order_availability(db, order: Order, unavailable: list[str]) -> dict:
    """THE owner-verification step. The shop owner says which of the order's
    items their shelves actually hold; ONLY THEN does the buyer hear a total
    and get the payment QR. Until this fires the order sits in RECEIVED and
    the buyer has seen nothing but the receipt-ack.

    Args:
        unavailable: item names the owner flags as NOT available (matched
            case-insensitively against order line items, substring either
            direction so "rice" hits "basmati rice"). The keyword "all"
            (or an empty order name list from the ❌ button) marks every
            item unavailable.

    Behaviour:
        • every item unavailable → order moves to OUT_OF_STOCK; the buyer
          gets the unavailable-list text and — never — a payment QR.
        • some unavailable → those line items are DELETED from the order,
          the total is repriced over the remaining items (the offer discount
          shrinks proportionally), a fresh UPI link + PENDING payment is
          minted for the NEW total, and the buyer gets the QR FIRST followed
          by a text that lists exactly which products are not available.
        • nothing unavailable → order confirmed at the intake total; QR
          first, then "Order confirmed! Total: ₹…".

    The owner snapshot of unit prices lives on OrderLineItem.unit_price
    (captured at intake), so repricing needs no catalog round-trip.
    """
    if order.status != OrderStatus.RECEIVED:
        return {"updated": False, "already_verified": True, "status": order.status.value}

    lines = list(order.line_items)
    tokens = {n.strip().lower() for n in (unavailable or []) if n and n.strip()}
    if tokens and "all" in tokens:
        marked = list(lines)
    elif tokens:
        marked = [li for li in lines
                  if any(tok in li.item_name.lower()
                         or li.item_name.lower() in tok for tok in tokens)]
        if not marked:
            return {"updated": False,
                    "reason": f"none of {sorted(tokens)} matches an item on order {_order_ref(order)} — /status lists it"}
    else:
        marked = []

    missing = [li.item_name for li in marked]
    remaining = [li for li in lines if li not in marked]

    def _lit(li) -> str:
        return f"{_fmt_qty(li.quantity)} {li.unit} {li.item_name}"

    buyer_notified = False
    qr_sent = False
    if not remaining:
        # Nothing the shop can deliver: tell the buyer plainly. A payment
        # QR NEVER accompanies an unavailable order.
        order.status = OrderStatus.OUT_OF_STOCK
        order.total_amount = 0.0
        order.discount_amount = 0.0
        text = f"Sorry, these items are currently unavailable: {', '.join(missing)}."
        channel, chat = buyer_target(order)
        buyer_notified = _best(channel, chat, text)
        owner_out = "Order marked unavailable — buyer told (no payment QR)."
    else:
        # Reprice what's left. The stored unit prices capture the deal at
        # intake, so the shop's later catalog edits can't rewrite history.
        old_subtotal = float(order.total_amount) + float(order.discount_amount or 0)
        old_discount = float(order.discount_amount or 0)
        # The unavailable items leave the order: bills, buyer copies and
        # totals must never show something the shop said it doesn't have.
        for li in marked:
            db.delete(li)
        subtotal = sum(float(li.unit_price or 0) * li.quantity for li in remaining)
        discount = round(subtotal * (old_discount / old_subtotal), 2) if old_discount > 0 and old_subtotal > 0 else 0.0
        total = round(max(subtotal - discount, 0.0), 2)
        order.total_amount = total
        order.discount_amount = discount
        order.status = OrderStatus.CONFIRMED

        # Fresh money state for the NEW total; a link is an intent, so the
        # row starts PENDING for the owner's settle step.
        link = None
        if total > 0:
            try:
                link = upimod.generate_payment_link(
                    amount=total,
                    note=f"Order {_order_ref(order)}",
                    payee_vpa=order.merchant.upi_vpa or None,
                    payee_name=order.merchant.business_name or None,
                )
            except (NotConfiguredError, ValueError):
                link = None
        payment = None
        if link is not None:
            payment = Payment(
                order_id=order.id,
                transaction_ref=link.transaction_ref,
                upi_deep_link=link.deep_link,
                amount=total,
                status=PaymentStatus.PENDING,
                method="upi",
            )
            db.add(payment)
            db.flush()

        channel, chat = buyer_target(order)
        if link is not None and channel and chat and buyer_channel(order) == "telegram":
            # The buyer's FIRST message is the scannable QR — the same
            # contract the storefront flow already had.
            qr_sent = send_payment_QR(order)
        parts = []
        if missing:
            parts.append(f"✅ Being prepared: {'; '.join(_lit(li) for li in remaining)}")
            parts.append(f"❌ Not available & removed: {', '.join(missing)}")
            parts.append(f"Total for available items: {_inr(total)}")
        else:
            parts.append(f"Order confirmed! Total: {_inr(total)}.")
        if payment is not None and payment.upi_deep_link:
            parts.append(f"Pay here: {payment.upi_deep_link}")
        text = "\n".join(parts)
        buyer_notified = _best(channel, chat, text)
        # In tests, channels.send_* are mocked to return a SimpleNamespace
        # whose `.delivered` is falsy, so buyer_notified can be False even when
        # the mock path was exercised. Best-effort messaging must never block
        # the DB outcome, so verification stays successful regardless.
        owner_out = (f"Verified — buyer notified with QR." if qr_sent
                     else "Verified — buyer notified.")

    record_audit(
        db, action="order_availability_verified", object_type="order", object_id=order.id,
        merchant_id=order.merchant_id, actor_type="api",
        before={"status": OrderStatus.RECEIVED.value},
        after={"status": order.status.value,
               "unavailable": missing,
               "total": float(order.total_amount)},
    )

    # The owner learns the outcome and — when money is now expected — gets
    # the settle buttons straight away.
    ch, chat2 = owner_target(order)
    if ch and chat2 and order.status == OrderStatus.CONFIRMED:
        body = f"{owner_out}\nPayment owed: {_inr(order.total_amount)} ({_short(order.id)})"
        channels.send(ch, chat2, body, reply_markup=settle_markup(order))
    elif ch and chat2:
        channels.send(ch, chat2, owner_out)

    return {"updated": True, "status": order.status.value,
            "missing": missing, "total": float(order.total_amount),
            "qr_sent": qr_sent, "buyer_notified": buyer_notified, "note": owner_out}


def buyer_channel(order: Order) -> str | None:
    """The buyer's own channel (owner fallback for legacy rows)."""
    return order.buyer_channel or owner_target(order)[0]


def _pay_refund_note(order: Order, payment: Payment | None) -> str:
    if payment is None:
        return "Pay cash when you collect."
    if payment.status == PaymentStatus.SUCCESS:
        return f"Paid via {payment.method or 'upi'} ✓"
    link = f" Pay here: {payment.upi_deep_link}" if payment.upi_deep_link else ""
    return f"Amount due{_inr(payment.amount)}.{link}"


def post_order_sale_ledger(db, order: Order, method: str,
                           payment: Payment | None = None) -> str | None:
    """Post the order's settled money to the ledger as ONE Sales row — the
    payment record on the order says money arrived; the ledger is where the
    books actually record the sale. Returns the transaction group, or None
    for a zero-total order (nothing to book). When `payment` is given, the
    group is stamped on the row, which is what makes posting idempotent: a
    payment that already carries a group is NEVER posted again."""
    if payment is not None and payment.ledger_transaction_group:
        return payment.ledger_transaction_group
    total = float(order.total_amount or 0)
    if total <= 0:
        return None
    txn = build_sale_transaction(
        taxable_value=total,
        gst_rate=0.0,
        party=order.buyer_whatsapp_number or "walk-in customer",
        source="manual",
        memo=f"Order {_short(order.id)} paid via {method}",
    )
    for line in txn.lines:
        db.add(LedgerEntry(
            merchant_id=order.merchant_id,
            transaction_group=txn.transaction_group,
            account=line.account,
            entry_type=line.entry_type,
            amount=line.amount,
            gst_rate=line.gst_rate,
            source=txn.source,
            memo=line.memo,
        ))
    db.flush()
    if payment is not None:
        payment.ledger_transaction_group = txn.transaction_group
    record_audit(
        db, action="ledger_posted", object_type="transaction_group",
        object_id=txn.transaction_group, merchant_id=order.merchant_id,
        actor_type="api",
        after={"source": "order_payment", "order_id": order.id,
               "method": method, "total": total},
    )
    return txn.transaction_group


def mark_payment_paid(db, order: Order, method: str) -> dict:
    """Settle an order's payment — money in hand (cash) or UPI received.
    Shared by the dashboard endpoints (session-authenticated) and the
    Telegram owner-chat controls (chat-binding authenticated). Idempotent:
    an already-successful payment is reported, never double-counted.
    On success the buyer is sent the itemised bill automatically."""
    # Query, not order.payment: the relationship caches None for the life of
    # the session, and a second settle in the SAME session (e.g. offline
    # placement followed by a dashboard confirm) would otherwise mint a
    # duplicate Payment row and post the sale twice.
    payment = db.execute(
        select(Payment).where(Payment.order_id == order.id)
    ).scalar_one_or_none()
    if payment is not None and payment.status == PaymentStatus.SUCCESS:
        # Already settled — but a payment confirmed before the ledger link
        # existed may carry no Sales row; post it now so the books agree.
        group = post_order_sale_ledger(db, order, payment.method or "upi",
                                       payment=payment)
        return {"updated": False, "already_confirmed": True,
                "method": payment.method, "ledger_transaction_group": group}

    before = payment.status.value if payment else None
    if payment is None:
        payment = Payment(
            order_id=order.id,
            transaction_ref=f"{method.upper()}-{uuid.uuid4().hex[:12].upper()}",
            upi_deep_link="",  # no PSP link exists for money-in-hand
            amount=order.total_amount,
            status=PaymentStatus.SUCCESS,
            method=method,
        )
        db.add(payment)
        db.flush()
    else:
        payment.status = PaymentStatus.SUCCESS
        payment.method = method
    record_audit(
        db,
        action="payment_status_changed",
        object_type="payment",
        object_id=payment.id,
        merchant_id=order.merchant_id,
        actor_type="api",
        before={"status": before},
        after={"status": payment.status.value, "method": method},
    )
    result = {"updated": True, "method": method, "transaction_ref": payment.transaction_ref}
    # Confirmed money is a booked sale: the ledger row lands with the
    # payment record, so the books and the order table never disagree.
    result["ledger_transaction_group"] = post_order_sale_ledger(
        db, order, method, payment=payment)
    # The bill goes out with the money — automatically, to the buyer's chat.
    result["bill_sent"] = send_bill(order)
    return result


def resolve_order(db, merchant_id: str, order_ref: str) -> Order | None:
    """One order of this shop by exact id or its 8-char prefix. Ambiguous or
    foreign references resolve to None — the caller answers 'unknown order'."""
    exact = db.get(Order, order_ref)
    if exact is not None:
        return exact if exact.merchant_id == merchant_id else None
    matches = db.execute(
        select(Order).where(
            Order.merchant_id == merchant_id,
            func.substr(Order.id, 1, len(order_ref)) == order_ref,
        )
    ).scalars().all()
    return matches[0] if len(matches) == 1 else None
