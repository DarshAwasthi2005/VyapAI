"""The append-only audit trail: every money-touching decision writes an
immutable row — and a weird payload never blocks the business transaction."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditEvent, Base
from app.services.audit import record_audit


def _fresh_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_record_audit_roundtrip():
    db = _fresh_session()
    record_audit(
        db,
        action="ledger_posted",
        object_type="transaction_group",
        object_id="grp-1",
        merchant_id="m1",
        actor_type="api",
        after={"lines": 2},
    )
    db.commit()

    row = db.execute(select(AuditEvent)).scalar_one()
    assert row.action == "ledger_posted"
    assert row.object_type == "transaction_group"
    assert row.merchant_id == "m1"
    assert row.after_json and '"lines": 2' in row.after_json
    assert row.created_at is not None


def test_unserializable_payload_stringified_never_raises():
    """A non-JSON value must not blow up (or rollback) the entry it describes."""
    db = _fresh_session()
    event = record_audit(
        db,
        action="approval_decided",
        object_type="approval_request",
        object_id="a1",
        after={"weird": {1, 2}},
    )
    db.commit()
    assert event.after_json is not None
    assert "{1, 2}" in event.after_json  # the set was stringified, not dropped


def test_before_and_after_both_preserved():
    db = _fresh_session()
    record_audit(
        db,
        action="payment_status_changed",
        object_type="payment",
        object_id="p1",
        before={"status": "PENDING"},
        after={"status": "SUCCESS"},
    )
    db.commit()
    row = db.execute(select(AuditEvent)).scalar_one()
    assert "PENDING" in row.before_json
    assert "SUCCESS" in row.after_json
