from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.graph_b2c import build_b2c_graph
from app.db.models import Merchant, Order, OrderLineItem, OrderStatus, Payment, PaymentStatus
from app.db.session import get_db
from app.schemas import WhatsAppOrderWebhook

router = APIRouter(prefix="/b2c", tags=["b2c"])
_graph = build_b2c_graph()


@router.post("/merchants/{merchant_id}/orders")
def submit_order(merchant_id: str, payload: WhatsAppOrderWebhook, db: Session = Depends(get_db)):
    merchant = db.get(Merchant, merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant not found")

    # TODO(deploy): load real catalog/stock from a merchant_catalog table.
    stock_levels = {"sugar": 100.0, "soap": 50.0}
    unit_prices = {"sugar": 42.0, "soap": 30.0}

    result = _graph.invoke({
        "buyer_whatsapp_number": payload.from_number,
        "raw_message": payload.message_text,
        "stock_levels": stock_levels,
        "unit_prices": unit_prices,
    })

    order = Order(
        merchant_id=merchant_id,
        buyer_whatsapp_number=payload.from_number,
        raw_message=payload.message_text,
        status=OrderStatus.CONFIRMED if result.get("all_in_stock") else OrderStatus.OUT_OF_STOCK,
        requested_by=result.get("deadline_text"),
        total_amount=result.get("total_amount", 0.0),
    )
    db.add(order)
    db.flush()

    for li in result.get("line_items", []):
        db.add(OrderLineItem(
            order_id=order.id, item_name=li.item_name, quantity=li.quantity, unit=li.unit,
        ))

    if result.get("upi_deep_link"):
        db.add(Payment(
            order_id=order.id,
            transaction_ref=result["transaction_ref"],
            upi_deep_link=result["upi_deep_link"],
            amount=result["total_amount"],
            status=PaymentStatus.PENDING,
        ))

    return {
        "order_id": order.id,
        "status": order.status.value,
        "total_amount": order.total_amount,
        "upi_link": result.get("upi_deep_link"),
        "parse_confidence": result.get("parse_confidence"),
    }
