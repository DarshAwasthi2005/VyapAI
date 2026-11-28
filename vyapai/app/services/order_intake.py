"""Shared B2C order intake.

The dashboard posts orders over HTTP, but the same flow must run when a
shop texts its order in over Telegram or WhatsApp. One function, three
callers — and replies go back on the channel the order arrived on.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph_b2c import node_check_inventory, node_parse, node_price_order
from app.config import settings
from app.db.models import Merchant, Order, OrderLineItem, OrderStatus
from app.services import channels, fulfillment, inventory


def parse_and_create(db: Session, merchant: Merchant, from_number: str,
                     message_text: str, reply_channel: str,
                     offer_type: str | None = None, offer_value: float | None = None,
                     buyer_channel: str | None = None,
                     buyer_chat_id: str | None = None) -> dict:
    """Run the B2C agent graph on a message and persist the order (plus a
    pending payment when a UPI link came out). buyer_channel/buyer_chat_id
    carry the ORDERER's chat when it differs from the shop's linked chat
    (public storefront buyers); replies then split buyer↔owner. Returns a
    dict with the dashboard/webhook response fields — no HTTP types."""
    return _run_graph(db, merchant, from_number, message_text, reply_channel,
                      structured=None, offer_type=offer_type, offer_value=offer_value,
                      buyer_channel=buyer_channel, buyer_chat_id=buyer_chat_id)


def create_from_structured(db: Session, merchant: Merchant, from_number: str,
                           raw_message: str, line_items: list[dict],
                           reply_channel: str,
                           offer_type: str | None = None,
                           offer_value: float | None = None,
                           buyer_channel: str | None = None,
                           buyer_chat_id: str | None = None) -> dict:
    """Intake for media orders (voice note / order-list photo) whose items
    were structured upstream instead of parsed from text. Runs the same
    graph — inventory, pricing, UPI link, buyer reply — over the
    pre-structured line items, so every media order behaves exactly like a
    typed one on the dashboard."""
    return _run_graph(db, merchant, from_number, raw_message, reply_channel,
                      structured=line_items, offer_type=offer_type,
                      offer_value=offer_value,
                      buyer_channel=buyer_channel, buyer_chat_id=buyer_chat_id)


def create_offline(db: Session, merchant: Merchant, buyer_label: str,
                   message_text: str,
                   offer_type: str | None = None,
                   offer_value: float | None = None,
                   payment_method: str | None = None) -> dict:
    """A walk-in order placed at the counter. The shopkeeper IS the
    availability check, so there is no RECEIVED wait: every item is checked
    against inventory, in-stock items are handed over immediately (stock
    drops) and the order is CONFIRMED on the spot. payment_method ("cash" |
    "upi") settles it in the same breath — payment record on the order plus
    a Sales row in the ledger; leave it None to settle later from the
    dashboard's confirm button. Raises ValueError when nothing in the
    message parses to an item."""
    stock_levels, unit_prices = inventory.load_catalog(db, merchant)

    state = {
        "buyer_whatsapp_number": buyer_label,
        "raw_message": message_text,
        "stock_levels": stock_levels,
        "unit_prices": unit_prices,
        "offer_type": offer_type if offer_type in ("percent", "cash") else None,
        "offer_value": offer_value if offer_value and offer_value > 0 else None,
    }
    result = node_parse(state)
    result = node_check_inventory(result)
    if not result.get("line_items"):
        raise ValueError("No items recognized in the order text — write it as "
                         "\"3 kg sugar and 2 soap\"")

    in_stock_flags = {r.item_name: r.in_stock
                      for r in result.get("inventory_results", [])}
    in_stock_items = [li for li in result["line_items"] if in_stock_flags.get(li.item_name)]
    unavailable = [li.item_name for li in result["line_items"]
                   if not in_stock_flags.get(li.item_name)]

    price_result = node_price_order({
        "unit_prices": unit_prices,
        "line_items": in_stock_items,
        "offer_type": state["offer_type"],
        "offer_value": state["offer_value"],
    })
    total = price_result.get("total_amount", 0.0)
    discount = price_result.get("discount_amount", 0.0)

    order = Order(
        merchant_id=merchant.id,
        buyer_whatsapp_number=buyer_label,
        # "offline" marks a walk-in: buyer-facing sends on it no-op safely,
        # and it never falls back to the owner's chat (there is no buyer to
        # message — the goods went over the counter).
        buyer_channel="offline",
        buyer_chat_id=None,
        raw_message=message_text,
        status=OrderStatus.CONFIRMED if in_stock_items else OrderStatus.OUT_OF_STOCK,
        requested_by=result.get("deadline_text"),
        total_amount=total,
        offer_type=state["offer_type"],
        offer_value=(state["offer_value"] if state["offer_value"] else None),
        discount_amount=discount,
    )
    db.add(order)
    db.flush()

    for li in in_stock_items:
        db.add(OrderLineItem(
            order_id=order.id, item_name=li.item_name, quantity=li.quantity,
            unit=li.unit, unit_price=unit_prices.get(li.item_name.lower()),
            in_stock=True,
        ))
    # The goods leave the shelf with the order: stock drops for what was
    # handed over, exactly the in-stock lines above.
    if in_stock_items:
        inventory.release_items(db, merchant.id, in_stock_items)

    payment_status = None
    ledger_group = None
    if payment_method and in_stock_items:
        settled = fulfillment.mark_payment_paid(db, order, payment_method)
        payment_status = "success"
        ledger_group = settled.get("ledger_transaction_group")

    return {
        "order_id": order.id,
        "status": order.status.value,
        "total_amount": float(order.total_amount),
        "discount_amount": float(discount),
        "offer": (f"{order.offer_type}:{order.offer_value}" if order.offer_type else None),
        "unavailable": unavailable,
        "payment_status": payment_status,
        "payment_method": payment_method,
        "ledger_transaction_group": ledger_group,
        "parse_confidence": result.get("parse_confidence"),
    }


def _run_graph(db: Session, merchant: Merchant, from_number: str,
               message_text: str, reply_channel: str,
               structured: list[dict] | None = None,
               offer_type: str | None = None,
               offer_value: float | None = None,
               buyer_channel: str | None = None,
               buyer_chat_id: str | None = None) -> dict:
    # Stock and prices live in the inventory_items table (seeded with the
    # demo catalog on first use); intake checks against it but does NOT
    # decrement — the owner-verification step decides what actually leaves
    # the shelf for a channel order.
    stock_levels, unit_prices = inventory.load_catalog(db, merchant)

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
        state["structured_items"] = structured    # Run the B2C graph nodes: parse, check inventory, then price.
    # The graph would normally route based on all_in_stock, but intake
    # always prices and lets the owner verify via fulfillment.
    # However, we still check inventory here so items unavailable at
    # intake time are filtered out; the owner can later mark more OOS.
    result = node_parse(state)
    result = node_check_inventory(result)
    result = node_price_order(result)

    # Filter line_items to only those that are in stock, so the order
    # only contains what the shop can actually deliver.
    in_stock_items = [
        li for li in result.get("line_items", [])
        if result.get("inventory_results", []) and any(
            r.item_name == li.item_name and r.in_stock for r in result["inventory_results"]
        )
    ]
    # Recompute total/discount over only the in-stock items — with the offer,
    # so the discount survives the filtered re-price.
    price_state = {
        "unit_prices": state["unit_prices"],
        "line_items": in_stock_items,
        "offer_type": state["offer_type"],
        "offer_value": state["offer_value"],
    }
    # Re-run price_order on the filtered items
    from app.agents.graph_b2c import node_price_order as _node_price_order
    price_result = _node_price_order(price_state)
    # Update result with filtered pricing
    result["total_amount"] = price_result.get("total_amount", 0.0)
    result["discount_amount"] = price_result.get("discount_amount", 0.0)

    # Buyer identity: an order placed from the SHOP's own chat is handled by
    # the owner (legacy single-chat flow); a buyer ordering from the bot's
    # storefront chat supplies their own chat, which buyer-facing replies
    # (order copy, ready notice, bill) must go back to.
    buyer_channel = buyer_channel or reply_channel
    buyer_chat = buyer_chat_id or (
        merchant.whatsapp_chat_id if reply_channel == "whatsapp"
        else merchant.telegram_chat_id
    )
    # A PUBLIC buyer is one whose chat was passed in explicitly by the
    # storefront webhook path — they are not the shop's own linked chat.
    # Such an order is auto-verified below by the intake AI when the shop
    # trusts its inventory table (settings.auto_confirm_public_orders):
    # the buyer gets the UPI QR within seconds instead of waiting for the
    # owner to tap "available".
    is_public_buyer = bool(buyer_chat_id) and buyer_chat != (
        merchant.telegram_chat_id if reply_channel == "telegram"
        else merchant.whatsapp_chat_id
    )

    if is_public_buyer and not (structured or result.get("line_items")):
        # A public buyer's chatter ("hi", "thanks", a stray emoji) is not an
        # order — create nothing, just show them how to order. Owner chats
        # keep the legacy behaviour (the shop is testing its own bot).
        channels.send(
            reply_channel, buyer_chat,
            "I couldn't find any items in that message. Write your order "
            "like: “2 kg chini and 5 soap” — or send a voice note / photo "
            "of your list.",
        )
        return {
            "order_id": None,
            "status": "not_an_order",
            "total_amount": 0.0,
            "discount_amount": 0.0,
            "offer": None,
            "upi_link": None,
            "qr_sent": False,
            "parse_confidence": result.get("parse_confidence"),
            "notify_error": None,
        }

    offer_type = state["offer_type"]
    offer_value = state["offer_value"]
    order = Order(
        merchant_id=merchant.id,
        buyer_whatsapp_number=from_number,
        buyer_channel=buyer_channel,
        buyer_chat_id=buyer_chat,
        raw_message=message_text,
        # Awaiting the OWNER's availability verification (see fulfillment
        # module docstring): the buyer gets only a receipt-ack now.
        status=OrderStatus.RECEIVED,
        requested_by=result.get("deadline_text"),
        total_amount=result.get("total_amount", 0.0),
        offer_type=offer_type,
        offer_value=(offer_value if offer_value and offer_value > 0 else None),
        discount_amount=result.get("discount_amount", 0.0),
    )
    db.add(order)
    db.flush()

    for li in result.get("line_items", []):
        # unit_price is captured per line so the automatic bill can itemise
        # @ ₹price — and so price history survives catalog changes.
        db.add(OrderLineItem(
            order_id=order.id, item_name=li.item_name, quantity=li.quantity, unit=li.unit,
            unit_price=unit_prices.get(li.item_name.lower()),
            in_stock=next((r.in_stock for r in result.get("inventory_results", [])
                           if r.item_name == li.item_name), None),
        ))
    # The order row and its line items must already be in the DB before any
    # later code reads the relationships lazily. The session is
    # autoflush=False (see app/db/session.py): adding objects here does NOT
    # auto-persist them, so a lazy load of order.line_items would return an
    # empty list and send every public buyer's in-stock order to
    # OUT_OF_STOCK with an empty "unavailable: ." message. Flush once.
    db.flush()

    # total in fulfillment.verify_order_availability, after the owner says
    # which items their shelves actually hold — or, for PUBLIC storefront
    # buyers, right here: the AI plays the owner using the shop's inventory
    # table as the shelves.
    summary = "; ".join(
        f"{li.quantity:g} {li.unit} {li.item_name}" for li in result.get("line_items", [])
    )
    result["error"] = None
    ack_ok = False
    auto_verified = None
    if is_public_buyer and settings.auto_confirm_public_orders:
        # Trust-inventory mode: whatever the catalog can serve stays; anything
        # the shelves lack is pruned and repriced exactly like the owner's
        # /missing step would. Fully OOS → OUT_OF_STOCK reply, no QR.
        oos_names = [
            row.item_name for row in result.get("inventory_results", [])
            if not row.in_stock
        ]
        auto_verified = fulfillment.verify_order_availability(db, order, oos_names)
        result["total_amount"] = float(order.total_amount)
        result["discount_amount"] = float(order.discount_amount or 0)
        # The goods are committed to the buyer: drop the stock exactly as a
        # counter sale would, so a second order can't oversell the shelf.
        if order.status == OrderStatus.CONFIRMED:
            remaining = db.execute(
                select(OrderLineItem).where(OrderLineItem.order_id == order.id)
            ).scalars().all()
            inventory.release_items(db, merchant.id, remaining)
    else:
        if buyer_chat and reply_channel:
            ack_ok = bool(channels.send(
                reply_channel, buyer_chat,
                "🧾 Order received! "
                + (summary + ". " if summary else "")
                + "The shop is checking availability — you'll get the total "
                  "and payment QR as soon as it's confirmed.",
            ).delivered)
        fulfillment.notify_owner_of_order(
            db, order, buyer_label=from_number or "(dashboard)", summary=summary,
        )
        if not ack_ok:
            result["error"] = (
                "No buyer chat to reply to — order recorded without buyer notification."
            )
    if auto_verified is not None:
        result["auto_verified"] = True
        result["qr_sent"] = bool(auto_verified.get("qr_sent"))
        result["buyer_notified"] = bool(auto_verified.get("buyer_notified"))

    return {
        "order_id": order.id,
        "status": order.status.value,
        "total_amount": float(order.total_amount),
        "discount_amount": result.get("discount_amount", 0.0),
        "offer": (
            f"{order.offer_type}:{order.offer_value}" if order.offer_type else None
        ),
        "upi_link": None,  # minted at verification, for the VERIFIED total
        "qr_sent": bool(auto_verified and auto_verified.get("qr_sent")),  # ditto
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


def buyer_rate_limited(
    db: Session, channel: str, chat_id: str | None
) -> bool:
    """Spam guardrail for the public storefront: counts how many orders one
    buyer chat has started on a channel in the last hour and compares it to
    PUBLIC_ORDER_RATE_LIMIT_PER_HOUR (default 12). A flooder gets a polite
    bounce instead of N UPI links minted against the shop's name."""
    if not chat_id:
        return False
    recent = db.execute(
        select(func.count(Order.id)).where(
            Order.buyer_channel == channel,
            Order.buyer_chat_id == str(chat_id),
            Order.created_at >= datetime.utcnow() - timedelta(hours=1),
        )
    ).scalar_one()
    return recent >= settings.public_order_rate_limit_per_hour
