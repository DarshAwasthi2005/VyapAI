"""
B2C agent graph: parse a WhatsApp order -> check inventory -> confirm or flag
out-of-stock items -> generate a UPI link -> send confirmation over WhatsApp.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.integrations import upi, whatsapp
from app.integrations.errors import NotConfiguredError, UpstreamError
from app.services.orders import (
    InventoryCheckResult, ParsedLineItem, check_inventory, parse_order_text,
)


class B2CState(TypedDict, total=False):
    buyer_whatsapp_number: str
    raw_message: str
    unit_prices: dict[str, float]  # item_name.lower() -> price, from merchant catalog
    stock_levels: dict[str, float]
    line_items: list[ParsedLineItem]
    deadline_text: str | None
    parse_confidence: str
    inventory_results: list[InventoryCheckResult]
    all_in_stock: bool
    total_amount: float
    upi_deep_link: str | None
    transaction_ref: str | None
    error: str | None


def node_parse(state: B2CState) -> B2CState:
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
    state["total_amount"] = round(total, 2)
    return state


def node_generate_payment_link(state: B2CState) -> B2CState:
    try:
        link = upi.generate_payment_link(
            amount=state["total_amount"],
            note=f"Order: {state['raw_message'][:40]}",
        )
        state["upi_deep_link"] = link.deep_link
        state["transaction_ref"] = link.transaction_ref
    except (NotConfiguredError, ValueError) as e:
        state["error"] = str(e)
    return state


def node_notify_buyer(state: B2CState) -> B2CState:
    try:
        if state.get("all_in_stock") and state.get("upi_deep_link"):
            msg = (
                f"Order confirmed! Total: ₹{state['total_amount']:,.2f}. "
                f"Pay here: {state['upi_deep_link']}"
            )
        else:
            oos = [r.item_name for r in state.get("inventory_results", []) if not r.in_stock]
            msg = f"Sorry, these items are currently unavailable: {', '.join(oos)}. Rest is being prepared."
        whatsapp.send_text_message(state["buyer_whatsapp_number"], msg)
    except (NotConfiguredError, UpstreamError) as e:
        state["error"] = str(e)
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
