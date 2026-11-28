"""Offline (walk-in) orders: stock check against the inventory table, items
handed over (stock drops), payment settled at placement — and every payment
confirmation (offline or not) books a Sales row in the ledger.
"""
from __future__ import annotations

import os

# Must be set before app.db.session is imported (engine is module-level).
os.environ.setdefault("DATABASE_URL", "sqlite:///./vyapai-test-offline.db")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, InventoryItem, LedgerEntry, Merchant, Order, Payment
from app.enums import OrderStatus, PaymentStatus
from app.services import fulfillment, order_intake


@pytest.fixture()
def db():
    tmp = os.path.join(os.path.dirname(__file__), ".tmp-offline.db")
    try:
        os.remove(tmp)
    except OSError:
        pass
    engine = create_engine(f"sqlite:///{tmp}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        try:
            os.remove(tmp)
        except OSError:
            pass


@pytest.fixture()
def shop(db) -> Merchant:
    m = Merchant(business_name="Counter Store", whatsapp_number="9876500099")
    db.add(m)
    db.commit()
    return m


def stock(db, merchant_id: str, item: str) -> float:
    row = db.execute(
        select(InventoryItem).where(
            InventoryItem.merchant_id == merchant_id,
            InventoryItem.item_name == item,
        )
    ).scalar_one()
    return row.quantity


def sales_ledger(db, merchant_id: str) -> list[LedgerEntry]:
    return db.execute(
        select(LedgerEntry).where(
            LedgerEntry.merchant_id == merchant_id,
            LedgerEntry.account == "Sales",
        )
    ).scalars().all()


def test_offline_order_confirms_decrements_stock_and_books_ledger(db, shop):
    out = order_intake.create_offline(
        db, shop, "9811100001", "3 kg sugar and 2 soap", payment_method="cash",
    )
    assert out["status"] == OrderStatus.CONFIRMED.value
    assert out["total_amount"] == pytest.approx(3 * 57.0 + 2 * 30.0)
    assert out["payment_status"] == "success"
    assert out["payment_method"] == "cash"
    assert out["ledger_transaction_group"]

    order = db.get(Order, out["order_id"])
    assert order.status == OrderStatus.CONFIRMED
    assert order.buyer_channel == "offline"

    # Items were handed over: stock drops by exactly what left the shelf.
    assert stock(db, shop.id, "sugar") == pytest.approx(97.0)
    assert stock(db, shop.id, "soap") == pytest.approx(48.0)

    payment = db.execute(
        select(Payment).where(Payment.order_id == order.id)
    ).scalar_one()
    assert payment.status == PaymentStatus.SUCCESS
    assert payment.method == "cash"
    assert float(payment.amount) == pytest.approx(out["total_amount"])

    entries = sales_ledger(db, shop.id)
    assert len(entries) == 1
    assert float(entries[0].amount) == pytest.approx(out["total_amount"])
    assert order.id[:8] in entries[0].memo


def test_offline_order_excludes_out_of_stock_items(db, shop):
    out = order_intake.create_offline(
        db, shop, "9811100002", "2 kg sugar and 999 bags of soap",
    )
    # Only what the shelf holds leaves the shop.
    assert out["status"] == OrderStatus.CONFIRMED.value
    assert out["unavailable"] == ["soap"]
    assert out["total_amount"] == pytest.approx(2 * 57.0)
    assert stock(db, shop.id, "soap") == pytest.approx(50.0)  # untouched
    assert stock(db, shop.id, "sugar") == pytest.approx(98.0)

    order = db.get(Order, out["order_id"])
    assert [li.item_name for li in order.line_items] == ["sugar"]


def test_offline_order_settles_later_via_payment_confirmation(db, shop):
    out = order_intake.create_offline(db, shop, "9811100003", "1 kg rice")
    assert out["payment_status"] is None
    assert sales_ledger(db, shop.id) == []

    order = db.get(Order, out["order_id"])
    result = fulfillment.mark_payment_paid(db, order, "upi")
    assert result["updated"] is True
    assert result["ledger_transaction_group"]

    entries = sales_ledger(db, shop.id)
    assert len(entries) == 1
    assert float(entries[0].amount) == pytest.approx(57.0)

    # Idempotent: confirming again never books a second sale.
    again = fulfillment.mark_payment_paid(db, order, "upi")
    assert again["already_confirmed"] is True
    assert len(sales_ledger(db, shop.id)) == 1


def test_offline_order_with_nothing_parsable_is_refused(db, shop):
    with pytest.raises(ValueError):
        order_intake.create_offline(db, shop, "9811100004", "hello, just looking")


def test_offline_order_when_nothing_in_stock_marks_out_of_stock(db, shop):
    # Seed the catalog first, then empty the sugar shelf (row kept at 0 so
    # ensure_catalog does not re-seed).
    order_intake.create_offline(db, shop, "9811100006", "1 kg rice")
    row = db.execute(
        select(InventoryItem).where(
            InventoryItem.merchant_id == shop.id, InventoryItem.item_name == "sugar"
        )
    ).scalar_one()
    row.quantity = 0.0
    db.flush()
    out = order_intake.create_offline(db, shop, "9811100005", "2 kg sugar")
    assert out["status"] == OrderStatus.OUT_OF_STOCK.value
    assert out["total_amount"] == 0
    assert out["payment_status"] is None
    assert sales_ledger(db, shop.id) == []


def test_inventory_seed_matches_demo_catalog_once(db, shop):
    order_intake.create_offline(db, shop, "9811100006", "1 kg sugar")
    # The first order seeds the demo catalog; a second order must not re-seed
    # (stock would reset) — it builds on whatever is left.
    order_intake.create_offline(db, shop, "9811100006", "1 kg sugar")
    assert stock(db, shop.id, "sugar") == pytest.approx(98.0)


def test_payment_confirmed_without_ledger_gets_backposted_once(db, shop):
    """A payment confirmed before the ledger link existed (no group stamped,
    no Sales row) must get its Sales row on the next confirm — exactly once."""
    import uuid as _uuid

    out = order_intake.create_offline(db, shop, "9811100007", "2 kg sugar")
    order = db.get(Order, out["order_id"])
    # Simulate the legacy shape: settled payment, no ledger row, no group.
    db.add(Payment(
        order_id=order.id,
        transaction_ref=f"LEGACY-{_uuid.uuid4().hex[:8].upper()}",
        upi_deep_link="",
        amount=order.total_amount,
        status=PaymentStatus.SUCCESS,
        method="cash",
    ))
    db.flush()
    assert sales_ledger(db, shop.id) == []

    first = fulfillment.mark_payment_paid(db, order, "cash")
    assert first["already_confirmed"] is True
    assert first["ledger_transaction_group"]
    entries = sales_ledger(db, shop.id)
    assert len(entries) == 1
    assert float(entries[0].amount) == pytest.approx(114.0)

    # The stamped group makes every later replay a no-op.
    second = fulfillment.mark_payment_paid(db, order, "cash")
    assert second["ledger_transaction_group"] == first["ledger_transaction_group"]
    assert len(sales_ledger(db, shop.id)) == 1


def test_normal_confirmation_stamps_group_and_never_double_posts(db, shop):
    out = order_intake.create_offline(db, shop, "9811100008", "1 kg sugar",
                                      payment_method="cash")
    payment = db.execute(
        select(Payment).where(Payment.order_id == out["order_id"])
    ).scalar_one()
    assert payment.ledger_transaction_group == out["ledger_transaction_group"]

    replay = fulfillment.mark_payment_paid(db, order := db.get(Order, out["order_id"]), "cash")
    assert replay["already_confirmed"] is True
    assert replay["ledger_transaction_group"] == payment.ledger_transaction_group
    assert len(sales_ledger(db, shop.id)) == 1


@pytest.fixture()
def api(db):
    """TestClient over ONLY the webhooks router, with our test session wired
    in as get_db — same commit-on-success semantics as the real dependency."""
    from app.api import webhooks
    from app.db.session import get_db

    probe = FastAPI()
    probe.include_router(webhooks.router)

    def override_get_db():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    probe.dependency_overrides[get_db] = override_get_db
    client = TestClient(probe)
    try:
        yield client
    finally:
        probe.dependency_overrides.clear()


def test_psp_webhook_books_ledger_and_replay_never_double_posts(db, shop, api):
    """The PSP "success" callback must book the Sales row, and a replayed
    callback must not book it twice."""
    from app.services import auth as authsvc

    out = order_intake.create_offline(db, shop, "9811100009", "2 kg rice")
    order = db.get(Order, out["order_id"])
    # A pending UPI payment, as verification would have minted it.
    db.add(Payment(
        order_id=order.id, transaction_ref="PSP-TEST-1",
        upi_deep_link="upi://pay?am=114.00", amount=order.total_amount,
        status=PaymentStatus.PENDING, method="upi",
    ))
    _session, token = authsvc.create_session(db, shop.id)
    db.commit()

    r = api.post("/webhooks/payments/psp",
                 headers={"Authorization": f"Bearer {token}"},
                 json={"transaction_ref": "PSP-TEST-1", "status": "success"})
    assert r.status_code == 200
    assert r.json()["ledger_transaction_group"]
    entries = sales_ledger(db, shop.id)
    assert len(entries) == 1
    assert float(entries[0].amount) == pytest.approx(114.0)

    # Replay: same answer, still exactly one Sales row.
    r = api.post("/webhooks/payments/psp",
                 headers={"Authorization": f"Bearer {token}"},
                 json={"transaction_ref": "PSP-TEST-1", "status": "success"})
    assert r.status_code == 200
    assert len(sales_ledger(db, shop.id)) == 1
