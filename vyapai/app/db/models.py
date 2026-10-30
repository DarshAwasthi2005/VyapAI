from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, Numeric, String, Text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import ApprovalStatus, EntryType, OrderStatus, PaymentStatus

# Re-exported so existing `from app.db.models import EntryType` style imports
# elsewhere keep working; the canonical definitions live in app/enums.py.
__all__ = [
    "Base", "Merchant", "LedgerEntry", "Order", "OrderLineItem", "Payment",
    "LoanApplicationRecord", "ApprovalRequest", "AuditEvent",
    "LoginChallenge", "AuthSession", "MerchantChannelPreference",
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
    # Second login identity: a shop may sign in (and be created) with an
    # email address instead of a phone number. Nullable — at least one
    # identity is required, both can coexist. Unique when set (SQLite keeps
    # uniqueness on the column; duplicates are also guarded in the API).
    email: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    upi_vpa: Mapped[str | None] = mapped_column(String, nullable=True)
    preferred_language: Mapped[str] = mapped_column(String, default="hi")
    # --- auth (real login, replaces the demo shop-chip switching) ----------
    # Login identity is the whatsapp_number column above: it is required at
    # registration and stays unique per shop.
    # PBKDF2-HMAC-SHA256, per-merchant salt. Never store the PIN itself.
    pin_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    pin_salt: Mapped[str | None] = mapped_column(String, nullable=True)
    # Telegram chat id of the merchant once they message /start <code> to the
    # bot — used to deliver login OTPs. None = Telegram 2FA not linked yet.
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # WhatsApp chat of the SHOP itself, linked on first contact: whenever an
    # inbound WhatsApp message arrives from the shop's own registered number,
    # the webhook stores it here — after that, buyer messages sent to that
    # number are routed to this shop as orders.
    whatsapp_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="merchant")
    orders: Mapped[list["Order"]] = relationship(back_populates="merchant")


class LoginChallenge(Base):
    """One short-lived challenge = one pending authentication step.
    purpose="login" holds a hashed 6-digit OTP delivered via Telegram (or
    echoed back in dev when Telegram is unconfigured). purpose="telegram_link"
    holds a pairing code the merchant sends to the bot as /start <code> to
    link their chat. Challenges are single-use and consumed on success."""
    __tablename__ = "login_challenges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    purpose: Mapped[str] = mapped_column(String)  # "login" | "telegram_link"
    # sha256 hex of the OTP / pairing code — plaintext never hits the DB.
    secret_hash: Mapped[str] = mapped_column(String)
    delivery: Mapped[str] = mapped_column(String, default="telegram")  # telegram | dev_echo
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    """A validated login. The raw bearer token is returned to the merchant
    exactly once; the DB stores only its sha256 digest, so a DB leak does not
    hand out live sessions. Expired/revoked sessions are rejected on use."""
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class LedgerEntry(Base):
    """Double-entry bookkeeping line. Every business event writes >=2 rows
    (one debit, one balancing credit) sharing the same transaction_group."""
    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    transaction_group: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)  # e.g. "Sales", "Cash", "Input GST Credit"
    entry_type: Mapped[EntryType] = mapped_column(Enum(EntryType))
    # Money columns are Numeric(14,2): a ledger must be exact; floats drift.
    # Python-side money math lives in app/utils/money.py (integer paise).
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
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
    # Offer the merchant gave on this bill — either a percent off or a flat
    # cash amount. discount_amount is the resulting reduction (≥ 0).
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    offer_type: Mapped[str | None] = mapped_column(String, nullable=True)   # "percent" | "cash"
    offer_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
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
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    # How the money actually arrived. "upi" = paid through the payment link
    # (a PSP webhook confirms it at go-live; in demo it confirms instantly);
    # "cash" = the merchant tapped "confirm cash" on the dashboard. Null =
    # unconfirmed/no payment row.
    method: Mapped[str | None] = mapped_column(String, nullable=True)  # "cash" | "upi"
    reminder_escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    order: Mapped["Order"] = relationship(back_populates="payment")


class LoanApplicationRecord(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
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


class MerchantChannelPreference(Base):
    """Per-shop choice of channel for a role. Roles: "otp" (login codes) and
    "orders" (buyer order intake + confirmations + reminders).
    Rows exist only for non-default choices; the default preference order for
    any role is telegram → whatsapp → none, restricted to channels actually
    configured/linked."""
    __tablename__ = "merchant_channel_preferences"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    role: Mapped[str] = mapped_column(String)   # "otp" | "orders"
    channel: Mapped[str] = mapped_column(String)  # "telegram" | "whatsapp"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditEvent(Base):
    """Immutable, attributable event log. Every money-touching decision — a
    ledger post, an approval decision, a payment-status change — writes a row
    here. Append-only by convention: nothing in this codebase updates or
    deletes AuditEvent rows, and no future feature should."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    merchant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String)  # "system" | "api" | "job"
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String)  # "ledger_posted" | "approval_decided" | ...
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
