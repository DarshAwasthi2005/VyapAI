"""money as paise + audit trail

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Float -> Numeric(14,2) for every money column. SQLite/Postgres-safe
    # surface types; on Postgres this is a real type change with implicit
    # value cast (values are already 2dp by convention).
    op.alter_column("ledger_entries", "amount",
                    type_=sa.Numeric(14, 2), existing_nullable=False)
    op.alter_column("orders", "total_amount",
                    type_=sa.Numeric(14, 2), existing_nullable=False)
    op.alter_column("payments", "amount",
                    type_=sa.Numeric(14, 2), existing_nullable=False)
    op.alter_column("loan_applications", "requested_amount",
                    type_=sa.Numeric(14, 2), existing_nullable=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, nullable=True, index=True),
        sa.Column("actor_type", sa.String, nullable=False),
        sa.Column("actor_id", sa.String, nullable=True),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("object_type", sa.String, nullable=False),
        sa.Column("object_id", sa.String, nullable=True),
        sa.Column("before_json", sa.Text, nullable=True),
        sa.Column("after_json", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.alter_column("loan_applications", "requested_amount",
                    type_=sa.Float, existing_nullable=False)
    op.alter_column("payments", "amount",
                    type_=sa.Float, existing_nullable=False)
    op.alter_column("orders", "total_amount",
                    type_=sa.Float, existing_nullable=False)
    op.alter_column("ledger_entries", "amount",
                    type_=sa.Float, existing_nullable=False)
