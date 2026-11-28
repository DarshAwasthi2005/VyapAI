"""inventory_items: per-shop stock + price for order intake

Order intake used a hardcoded stock/price dict; real orders need a table the
shop's sales actually move (offline orders hand items over, so quantity must
drop).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"),
                  nullable=False, index=True),
        sa.Column("item_name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unit_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("merchant_id", "item_name",
                            name="uq_inventory_merchant_item"),
    )


def downgrade() -> None:
    op.drop_table("inventory_items")
