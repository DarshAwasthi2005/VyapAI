"""Backfill the ledger for payments confirmed before the ledger link existed.

Every payment confirmation now books a Sales row and stamps the group on
Payment.ledger_transaction_group (migration 0007). Confirmed payments that
predate that change carry no group and no Sales row — this script posts the
missing row for each of them, once (idempotent: re-running does nothing).

Run from the repo root with the same DATABASE_URL the server uses:
    DATABASE_URL=sqlite:///./vyapai-dev.db python scripts/backfill_payment_ledger.py
"""
from __future__ import annotations

import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.db.session import SessionLocal, init_db
from app.db.models import Payment, PaymentStatus
from app.services import fulfillment


def main() -> int:
    init_db()  # reconcile the schema first: a pre-0007 DB lacks the column
    db = SessionLocal()
    try:
        payments = db.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.SUCCESS,
                Payment.ledger_transaction_group.is_(None),
            )
        ).scalars().all()
        posted, skipped = 0, 0
        for payment in payments:
            order = payment.order
            if order is None or float(order.total_amount or 0) <= 0:
                skipped += 1
                continue
            group = fulfillment.post_order_sale_ledger(
                db, order, payment.method or "upi", payment=payment)
            if group:
                print(f"posted ₹{float(payment.amount):,.2f} for order "
                      f"#{order.id[:8]} (payment {payment.id[:8]}) -> group {group[:8]}")
                posted += 1
        db.commit()
        print(f"done: {posted} Sales row(s) posted, {skipped} zero/unowned skipped.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
