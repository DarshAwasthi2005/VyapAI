"""
Append-only audit trail. One helper, used everywhere; deliberately boring.

Callers pass plain JSON-serializable dicts; anything unexpected is stringified
rather than raising, because a failed audit write must never block (or worse,
roll back) the business transaction it is describing.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditEvent


def _json_or_string(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def record_audit(
    db: Session,
    *,
    action: str,
    object_type: str,
    object_id: str | None = None,
    merchant_id: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> AuditEvent:
    """Write one immutable audit row on the caller's open session.

    The row is flushed so callers see its id; committing remains the caller's
    responsibility (it commits the business transaction and the audit row
    atomically together, which is the point).
    """
    event = AuditEvent(
        merchant_id=merchant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before_json=_json_or_string(before),
        after_json=_json_or_string(after),
        reason=reason,
    )
    db.add(event)
    db.flush()
    return event
