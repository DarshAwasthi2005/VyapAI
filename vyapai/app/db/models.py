from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import ApprovalStatus, EntryType, OrderStatus, PaymentStatus

# Re-exported so existing `from app.db.models import EntryType` style imports
# elsewhere keep working; the canonical definitions live in app/enums.py.
__all__ = [
    "Base", "Merchant", "LedgerEntry", "Order", "OrderLineItem", "Payment",
    "LoanApplicationRecord", "ApprovalRequest",
    "EntryType", "OrderStatus", "PaymentStatus", "ApprovalStatus",
]


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(String, nullable=False)
    gstin: Mapped[str | None] = mapped_column(String, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String, nullable=True)
    upi_vpa: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_language: Mapped[str] = mapped_column(String, default="hi")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="merchant")
    orders: Mapped[list["Order"]] = relationship(back_populates="merchant")


class LedgerEntry(Base):
    """Double-entry bookkeeping line. Every business event writes >=2 rows
    (one debit, one balancing credit) sharing the same transaction_group."""
    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    transaction_group: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)  # e.g. "Sales", "Cash", "Input GST Credit"
    entry_type: Mapped[EntryType] = mapped_column(Enum(EntryType))
    amount: Mapped[float] = mapped_column(Float)
    gst_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String)  # "voice_note" | "bill_photo" | "whatsapp_order" | "manual"
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    merchant: Mapped["Merchant"] = relationship(back_populates="ledger_entries")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    buyer_whatsapp_number: Mapped[str] = mapped_column(String)
    raw_message: Mapped[str] = mapped_column(Text)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.RECEIVED)
    requested_by: Mapped[str | None] = mapped_column(String, nullable=True)  # "by 4 PM" etc, as text
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    merchant: Mapped["Merchant"] = relationship(back_populates="orders")
    line_items: Mapped[list["OrderLineItem"]] = relationship(back_populates="order")
    payment: Mapped["Payment | None"] = relationship(back_populates="order", uselist=False)


class OrderLineItem(Base):
    __tablename__ = "order_line_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    item_name: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)  # "bags", "cartons", "kg"...
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="line_items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    transaction_ref: Mapped[str] = mapped_column(String, unique=True)
    upi_deep_link: Mapped[str] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    reminder_escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    order: Mapped["Order"] = relationship(back_populates="payment")


class LoanApplicationRecord(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    requested_amount: Mapped[float] = mapped_column(Float)
    purpose: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft->pending_approval->submitted->...
    ocen_application_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ApprovalRequest(Base):
    """Human-in-the-loop queue. Any agent action tagged as sensitive
    (e-way bill submission, loan application, large ledger correction)
    lands here instead of executing directly."""
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    action_type: Mapped[str] = mapped_column(String)  # "e_way_bill" | "loan_application" | "large_ledger_entry"
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
