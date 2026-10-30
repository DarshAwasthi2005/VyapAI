"""Shared B2C order intake.

The dashboard posts orders over HTTP, but the same flow must run when a
shop texts its order in over Telegram or WhatsApp. One function, three
callers — and replies go back on the channel the order arrived on.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.graph_b2c import build_b2c_graph, build_buyer_message
from app.db.models import Merchant, Order, OrderLineItem, OrderStatus, Payment, PaymentStatus
from app.services import channels

_graph = build_b2c_graph()


def parse_and_create(db: Session, merchant: Merchant, from_number: str,
                     message_text: str, reply_channel: str,
                     offer_type: str | None = None, offer_value: float | None = None) -> dict:
    """Run the B2C agent graph on a message and persist the order (plus a
    pending payment when a UPI link came out). Returns a dict with the
    dashboard/webhook response fields — no HTTP types involved."""
    return _run_graph(db, merchant, from_number, message_text, reply_channel,
                      structured=None, offer_type=offer_type, offer_value=offer_value)


def create_from_structured(db: Session, merchant: Merchant, from_number: str,
                           raw_message: str, line_items: list[dict],
                           reply_channel: str,
                           offer_type: str | None = None,
                           offer_value: float | None = None) -> dict:
    """Intake for media orders (voice note / order-list photo) whose items
    were structured upstream instead of parsed from text. Runs the same
    graph — inventory, pricing, UPI link, buyer reply — over the
    pre-structured line items, so every media order behaves exactly like a
    typed one on the dashboard."""
    return _run_graph(db, merchant, from_number, raw_message, reply_channel,
                      structured=line_items, offer_type=offer_type,
                      offer_value=offer_value)


def _run_graph(db: Session, merchant: Merchant, from_number: str,
               message_text: str, reply_channel: str,
               structured: list[dict] | None = None,
               offer_type: str | None = None,
               offer_value: float | None = None) -> dict:
    # TODO(deploy): load real catalog/stock from a merchant_catalog table.
    stock_levels = {"sugar": 100.0, "soap": 50.0}
    unit_prices = {"sugar": 42.0, "soap": 30.0}

    state = {
        "buyer_whatsapp_number": from_number,
        "raw_message": message_text,
        "stock_levels": stock_levels,
        "unit_prices": unit_prices,
        "reply_channel": reply_channel,
        "reply_chat_id": merchant.whatsapp_chat_id if reply_channel == "whatsapp" else merchant.telegram_chat_id,
        # UPI links must pay the SHOP's own registered UPI id
        # (merchant.upi_vpa); the global UPI_PAYEE_VPA env value is only a
        # last-resort fallback. Orders with neither come back upi_link=None
        # with a clear notify_error instead of a link that pays nobody.
        "payee_vpa": merchant.upi_vpa or None,
        "payee_name": merchant.business_name or None,
        "offer_type": offer_type if offer_type in ("percent", "cash") else None,
        "offer_value": offer_value if offer_value and offer_value > 0 else None,
    }
    if structured:
        state["structured_items"] = structured
    result = _graph.invoke(state)

    offer_type = state["offer_type"]
    offer_value = state["offer_value"]
    order = Order(
        merchant_id=merchant.id,
        buyer_whatsapp_number=from_number,
        raw_message=message_text,
        status=OrderStatus.CONFIRMED if result.get("all_in_stock") else OrderStatus.OUT_OF_STOCK,
        requested_by=result.get("deadline_text"),
        total_amount=result.get("total_amount", 0.0),
        offer_type=offer_type,
        offer_value=(offer_value if offer_value and offer_value > 0 else None),
        discount_amount=result.get("discount_amount", 0.0),
    )
    db.add(order)
    db.flush()

    for li in result.get("line_items", []):
        db.add(OrderLineItem(
            order_id=order.id, item_name=li.item_name, quantity=li.quantity, unit=li.unit,
        ))

    if result.get("upi_deep_link"):
        # UPI intent payments confirm the moment they are made (the buyer
        # pays inside their UPI app) — mark them directly confirmed, per the
        # merchant-facing behavior. At go-live the PSP webhook (Razorpay/
        # Cashfree) posts the authoritative status over this; cash orders
        # stay unconfirmed until the merchant taps "Confirm cash".
        db.add(Payment(
            order_id=order.id,
            transaction_ref=result["transaction_ref"],
            upi_deep_link=result["upi_deep_link"],
            amount=result["total_amount"],
            status=PaymentStatus.SUCCESS,
            method="upi",
        ))

    if reply_channel == "telegram":
        # The graph deliberately skipped its Telegram copy (node_notify_buyer
        # defers it): send it now that the order row exists, so the payment
        # buttons can carry the real order id in callback_data. Buttons only
        # while nothing is settled — a UPI-link payment already confirmed
        # itself above, and an out-of-stock order has nothing to pay.
        chat_id = merchant.telegram_chat_id
        if not chat_id:
            result["error"] = (
                "No chat channel linked for this shop — order recorded without "
                "buyer notification."
            )
        else:
            markup = None
            if result.get("all_in_stock") and order.payment is None:
                markup = {"inline_keyboard": [[
                    {"text": "💵 Cash received", "callback_data": f"cash:{order.id}"},
                    {"text": "✅ Paid via UPI", "callback_data": f"paid:{order.id}"},
                ]]}
            outcome = channels.send(
                "telegram", chat_id, build_buyer_message(result), reply_markup=markup
            )
            if not outcome.delivered:
                result["error"] = outcome.error

    return {
        "order_id": order.id,
        "status": order.status.value,
        "total_amount": float(order.total_amount),
        "discount_amount": result.get("discount_amount", 0.0),
        "offer": (
            f"{order.offer_type}:{order.offer_value}" if order.offer_type else None
        ),
        "upi_link": result.get("upi_deep_link"),
        "parse_confidence": result.get("parse_confidence"),
        "notify_error": result.get("error"),
    }


def resolve_merchant_for_chat(db: Session, channel: str, chat_token: str) -> Merchant | None:
    """Which shop does this chat belong to? Exact match on the LINKED chat
    (Telegram chat id / WhatsApp number). Unknown chats are never silently
    assigned to a shop."""
    column = Merchant.telegram_chat_id if channel == "telegram" else Merchant.whatsapp_chat_id
    return db.execute(
        select(Merchant).where(column == chat_token)
    ).scalar_one_or_none()
