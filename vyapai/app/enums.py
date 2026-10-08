"""
Plain-Python enums with zero dependencies. app/services/*.py imports these
directly so the core business logic (accounting, guardrails, orders,
collections) can be unit-tested without SQLAlchemy/FastAPI installed.
app/db/models.py wraps the same enums with SQLAlchemy's Enum() column type.
"""
from __future__ import annotations

import enum


class EntryType(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class OrderStatus(str, enum.Enum):
    RECEIVED = "received"
    CONFIRMED = "confirmed"
    OUT_OF_STOCK = "out_of_stock"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
