"""
B2B agent graph: ingest (voice or photo) -> extract structured entry ->
guardrail check -> post to ledger, OR route to human approval.

Requires `langgraph` (see requirements.txt). Built as a typed-dict state
graph per LangGraph's documented pattern. If you're running this before
`pip install -r requirements.txt`, import will fail — the pure business
logic it calls (accounting, guardrails) has no such dependency and is
tested independently in tests/.
"""
from __future__ import annotations

import json
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph

from app.agents.guardrails import validate_bill_extraction
from app.integrations import speech, vision
from app.integrations.errors import NotConfiguredError, UpstreamError
from app.services.accounting import build_purchase_transaction, build_sale_transaction


class B2BState(TypedDict, total=False):
    merchant_id: str
    input_mode: Literal["voice", "photo", "text"]
    audio_bytes: bytes | None
    image_bytes: bytes | None
    raw_text: str | None
    party_name: str | None
    line_items: list[tuple[float, float, float]]  # (qty, unit_price, amount)
    stated_total: float | None
    gst_rate: float | None
    transaction_kind: Literal["sale", "purchase"]
    guardrail_passed: bool
    guardrail_reasons: list[str]
    needs_human_approval: bool
    ledger_transaction_group: str | None
    error: str | None


def node_ingest(state: B2BState) -> B2BState:
    """Turn voice/photo/text input into a structured extraction."""
    try:
        if state["input_mode"] == "voice" and state.get("audio_bytes"):
            result = speech.transcribe(state["audio_bytes"])
            state["raw_text"] = result.text
            # A second LLM pass would structure raw_text into line items —
            # left to a text-completion call alongside vision.py's pattern.
        elif state["input_mode"] == "photo" and state.get("image_bytes"):
            extraction = vision.extract_bill(state["image_bytes"])
            state["party_name"] = extraction.party_name
            state["stated_total"] = extraction.stated_total
            state["line_items"] = [
                (li.quantity, li.unit_price, li.amount) for li in extraction.line_items
            ]
        # "text" mode: raw_text/line_items assumed already populated by caller
    except (NotConfiguredError, UpstreamError) as e:
        state["error"] = str(e)
    return state


def node_guardrail(state: B2BState) -> B2BState:
    if state.get("error"):
        state["guardrail_passed"] = False
        state["needs_human_approval"] = True
        state["guardrail_reasons"] = [state["error"]]
        return state

    result = validate_bill_extraction(
        line_items=state.get("line_items", []),
        stated_total=state.get("stated_total"),
        gst_rate=state.get("gst_rate"),
    )
    state["guardrail_passed"] = result.passed
    state["guardrail_reasons"] = result.reasons
    state["needs_human_approval"] = result.requires_human_approval
    return state


def node_post_ledger(state: B2BState) -> B2BState:
    taxable_value = sum(amount for _, _, amount in state.get("line_items", []))
    gst_rate = state.get("gst_rate") or 0.0
    party = state.get("party_name") or "Unknown"

    if state.get("transaction_kind", "sale") == "sale":
        txn = build_sale_transaction(
            taxable_value=taxable_value, gst_rate=gst_rate, party=party,
            source=state["input_mode"],
        )
    else:
        txn = build_purchase_transaction(
            taxable_value=taxable_value, gst_rate=gst_rate, party=party,
            source=state["input_mode"],
        )
    state["ledger_transaction_group"] = txn.transaction_group
    # Caller (api/routes_b2b.py) persists txn.lines via db/session.py.
    return state


def node_route_to_human(state: B2BState) -> B2BState:
    # Caller persists an ApprovalRequest row with state["guardrail_reasons"].
    return state


def _route_after_guardrail(state: B2BState) -> str:
    return "post_ledger" if state.get("guardrail_passed") else "route_to_human"


def build_b2b_graph():
    graph = StateGraph(B2BState)
    graph.add_node("ingest", node_ingest)
    graph.add_node("guardrail", node_guardrail)
    graph.add_node("post_ledger", node_post_ledger)
    graph.add_node("route_to_human", node_route_to_human)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "guardrail")
    graph.add_conditional_edges(
        "guardrail", _route_after_guardrail,
        {"post_ledger": "post_ledger", "route_to_human": "route_to_human"},
    )
    graph.add_edge("post_ledger", END)
    graph.add_edge("route_to_human", END)
    return graph.compile()
