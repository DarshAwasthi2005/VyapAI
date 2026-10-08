"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("business_name", sa.String, nullable=False),
        sa.Column("gstin", sa.String, nullable=True),
        sa.Column("whatsapp_number", sa.String, nullable=True),
        sa.Column("upi_vpa", sa.String, nullable=True),
        sa.Column("preferred_language", sa.String, server_default="hi"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("transaction_group", sa.String, index=True, nullable=False),
        sa.Column("account", sa.String, nullable=False),
        sa.Column("entry_type", sa.Enum("debit", "credit", name="entrytype"), nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("gst_rate", sa.Float, nullable=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("memo", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("buyer_whatsapp_number", sa.String, nullable=False),
        sa.Column("raw_message", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Enum("received", "confirmed", "out_of_stock", "delivered", "cancelled", name="orderstatus"),
            server_default="received",
        ),
        sa.Column("requested_by", sa.String, nullable=True),
        sa.Column("total_amount", sa.Float, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "order_line_items",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("order_id", sa.String, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("item_name", sa.String, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("unit", sa.String, nullable=False),
        sa.Column("unit_price", sa.Float, nullable=True),
        sa.Column("in_stock", sa.Boolean, nullable=True),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("order_id", sa.String, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("transaction_ref", sa.String, unique=True, nullable=False),
        sa.Column("upi_deep_link", sa.Text, nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("status", sa.Enum("pending", "success", "failed", name="paymentstatus"), server_default="pending"),
        sa.Column("reminder_escalation_level", sa.Integer, server_default="0"),
        sa.Column("last_reminder_sent_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "loan_applications",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("requested_amount", sa.Float, nullable=False),
        sa.Column("purpose", sa.String, nullable=False),
        sa.Column("status", sa.String, server_default="draft"),
        sa.Column("ocen_application_id", sa.String, nullable=True),
        sa.Column("approved_by_human", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("action_type", sa.String, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("status", sa.Enum("pending", "approved", "rejected", name="approvalstatus"), server_default="pending"),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("approval_requests")
    op.drop_table("loan_applications")
    op.drop_table("payments")
    op.drop_table("order_line_items")
    op.drop_table("orders")
    op.drop_table("ledger_entries")
    op.drop_table("merchants")
    sa.Enum(name="approvalstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="paymentstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="orderstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="entrytype").drop(op.get_bind(), checkfirst=True)
