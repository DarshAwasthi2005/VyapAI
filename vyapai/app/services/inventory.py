"""Per-shop stock (inventory_items table) shared by every order-intake path.

The table is seeded lazily with the demo catalog the first time a shop places
or receives an order, so existing shops keep the stock/price levels the
hardcoded dict used to promise.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InventoryItem, Merchant

# item_name -> (stock quantity, unit price). Values mirror the demo catalog
# order intake used before the table existed.
DEFAULT_CATALOG: dict[str, tuple[float, float]] = {
    "sugar": (100.0, 57.0),
    "soap": (50.0, 30.0),
    "rice": (45.0, 57.0),
    "maida": (50.0, 60.0),
}


def ensure_catalog(db: Session, merchant: Merchant) -> None:
    """Seed the demo catalog once per shop. Existing rows are never touched —
    a shop that has sold stock keeps its (lower) quantities."""
    existing = db.execute(
        select(InventoryItem.id).where(InventoryItem.merchant_id == merchant.id)
    ).first()
    if existing:
        return
    for name, (qty, price) in DEFAULT_CATALOG.items():
        db.add(InventoryItem(
            merchant_id=merchant.id, item_name=name,
            quantity=qty, unit_price=price,
        ))
    db.flush()


def load_catalog(db: Session, merchant: Merchant) -> tuple[dict[str, float], dict[str, float]]:
    """(stock_levels, unit_prices) keyed by lowercase item name."""
    ensure_catalog(db, merchant)
    rows = db.execute(
        select(InventoryItem).where(InventoryItem.merchant_id == merchant.id)
    ).scalars().all()
    return (
        {r.item_name: r.quantity for r in rows},
        {r.item_name: r.unit_price for r in rows},
    )


def release_items(db: Session, merchant_id: str, line_items: list) -> None:
    """Items handed over the counter: drop each line's quantity from stock.
    Never below zero (a race between two orders floors at 0), and unknown
    catalog names are skipped — there was no stock to give."""
    by_name = {
        r.item_name: r
        for r in db.execute(
            select(InventoryItem).where(InventoryItem.merchant_id == merchant_id)
        ).scalars().all()
    }
    for li in line_items:
        row = by_name.get(li.item_name.lower())
        if row is None:
            continue
        row.quantity = max(row.quantity - float(li.quantity), 0.0)
    db.flush()
