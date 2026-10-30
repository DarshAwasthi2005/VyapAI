"""
B2C agent graph: parse a WhatsApp order -> check inventory -> confirm or flag
out-of-stock items -> generate a UPI link -> send confirmation over WhatsApp.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.integrations import upi
from app.integrations.errors import NotConfiguredError
from app.services.orders import (
    InventoryCheckResult, ParsedLineItem, check_inventory, parse_order_text,
)


class B2CState(TypedDict, total=False):
    buyer_whatsapp_number: str
    raw_message: str
    unit_prices: dict[str, float]  # item_name.lower() -> price, from merchant catalog
    stock_levels: dict[str, float]
    structured_items: list[dict]  # photo/voice intake: {item_name, quantity, unit}
    line_items: list[ParsedLineItem]
    deadline_text: str | None
    parse_confidence: str
    inventory_results: list[InventoryCheckResult]
    all_in_stock: bool
    total_amount: float
    upi_deep_link: str | None
    transaction_ref: str | None
    reply_channel: str | None  # "telegram" | "whatsapp" — where to reply
    reply_chat_id: str | None  # chat/number to reply to on that channel
    offer_type: str | None     # "percent" | "cash" — bill discount the merchant grants
    offer_value: float | None
    discount_amount: float
    payee_vpa: str | None  # the SHOP's own UPI id — who the money goes to
    payee_name: str | None
    error: str | None


def node_parse(state: B2CState) -> B2CState:
    if state.get("structured_items"):
        # Media intake (order-photo OCR, pre-transcribed voice notes) hands
        # the graph items already structured — the text parser is skipped.
        state["line_items"] = [
            ParsedLineItem(
                item_name=d["item_name"],
                quantity=float(d["quantity"]),
                unit=str(d.get("unit", "units")),
            )
            for d in state["structured_items"]
        ]
        state["deadline_text"] = None
        state["parse_confidence"] = "high"
        return state
    parsed = parse_order_text(state["raw_message"])
    state["line_items"] = parsed.line_items
    state["deadline_text"] = parsed.deadline_text
    state["parse_confidence"] = parsed.parse_confidence
    return state


def node_check_inventory(state: B2CState) -> B2CState:
    class _Order:
        line_items = state["line_items"]
    results = check_inventory(_Order(), state.get("stock_levels", {}))
    state["inventory_results"] = results
    state["all_in_stock"] = all(r.in_stock for r in results) and len(results) > 0
    return state


def node_price_order(state: B2CState) -> B2CState:
    prices = state.get("unit_prices", {})
    total = 0.0
    for li in state["line_items"]:
        price = prices.get(li.item_name.lower(), 0.0)
        total += price * li.quantity
    # Offer (the person making the bill chooses percent OR flat cash):
    # clamped to keep the bill sane — a percent over 100 or a discount
    # bigger than the bill itself reduces it to zero, never negative.
    offer_type, offer_value = state.get("offer_type"), state.get("offer_value")
    discount = 0.0
    if offer_value and offer_value > 0:
        offer_value = float(offer_value)
        if offer_type == "percent":
            discount = total * min(max(offer_value, 0.0), 100.0) / 100.0
        elif offer_type == "cash":
            discount = max(offer_value, 0.0)
        discount = round(min(discount, total), 2)
    state["discount_amount"] = discount
    state["total_amount"] = round(max(total - discount, 0.0), 2)
    return state


def node_generate_payment_link(state: B2CState) -> B2CState:
    try:
        link = upi.generate_payment_link(
            amount=state["total_amount"],
            note=f"Order: {state['raw_message'][:40]}",
            payee_vpa=state.get("payee_vpa"),
            payee_name=state.get("payee_name"),
        )
        state["upi_deep_link"] = link.deep_link
        state["transaction_ref"] = link.transaction_ref
    except (NotConfiguredError, ValueError) as e:
        state["error"] = str(e)
    return state


def build_buyer_message(state: B2CState) -> str:
    """The buyer-facing reply for a completed run — shared by the notify
    node below (WhatsApp / dashboard-posted orders) and services/
    order_intake, which sends Telegram's copy AFTER the order row exists so
    its inline-keyboard buttons can reference the real order id."""
    if state.get("all_in_stock"):
        discount = state.get("discount_amount") or 0
        offer_notes = f" (after your offer: -₹{discount:,.2f})" if discount else ""
        msg = f"Order confirmed! Total: ₹{state['total_amount']:,.2f}{offer_notes}."
        if state.get("upi_deep_link"):
            msg += f" Pay here: {state['upi_deep_link']}"
        return msg
    oos = [r.item_name for r in state.get("inventory_results", []) if not r.in_stock]
    return f"Sorry, these items are currently unavailable: {', '.join(oos)}. Rest is being prepared."


def node_notify_buyer(state: B2CState) -> B2CState:
    """Confirm (or flag out-of-stock) on whichever channel the order came in
    on — the intake layer passes reply_channel/reply_chat_id through state.
    Failures never break the order result: messaging is best-effort via
    services/channels.send, which captures instead of raising.

    Telegram is deferred: its reply carries inline-keyboard payment buttons
    that must reference the persisted order id, so services/order_intake
    sends it right after the rows exist — same message, same chat."""
    from app.services import channels as _channels

    channel = state.get("reply_channel")
    chat_id = state.get("reply_chat_id")
    if channel == "telegram":
        return state  # sent post-persistence by the intake layer
    if not channel or not chat_id:
        # HTTP-posted order with no chat linkage: nothing to message into.
        state["error"] = "No chat channel linked for this shop — order recorded without buyer notification."
        return state

    outcome = _channels.send(channel, chat_id, build_buyer_message(state))
    if not outcome.delivered:
        state["error"] = outcome.error
    return state


def _route_after_inventory(state: B2CState) -> str:
    return "price_order" if state.get("all_in_stock") else "notify_buyer"


def build_b2c_graph():
    graph = StateGraph(B2CState)
    graph.add_node("parse", node_parse)
    graph.add_node("check_inventory", node_check_inventory)
    graph.add_node("price_order", node_price_order)
    graph.add_node("generate_payment_link", node_generate_payment_link)
    graph.add_node("notify_buyer", node_notify_buyer)

    graph.set_entry_point("parse")
    graph.add_edge("parse", "check_inventory")
    graph.add_conditional_edges(
        "check_inventory", _route_after_inventory,
        {"price_order": "price_order", "notify_buyer": "notify_buyer"},
    )
    graph.add_edge("price_order", "generate_payment_link")
    graph.add_edge("generate_payment_link", "notify_buyer")
    graph.add_edge("notify_buyer", END)
    return graph.compile()
