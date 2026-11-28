"""storefront_selections: each public buyer chat's chosen shop

When several shops share one bot, the buyer used to have to name their shop
in EVERY message. This table remembers the choice made once ("order from
shukla gift store") so later bare orders route to the same shop — naming a
different shop overwrites the row (one per buyer chat).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storefront_selections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("buyer_channel", sa.String(), nullable=False),
        sa.Column("buyer_chat_id", sa.String(), nullable=False, index=True),
        sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("buyer_channel", "buyer_chat_id",
                            name="uq_storefront_selection_chat"),
    )


def downgrade() -> None:
    op.drop_table("storefront_selections")
