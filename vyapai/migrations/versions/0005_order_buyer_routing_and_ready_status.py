"""orders: buyer routing columns (public buyers) + 'ready' status

The customer journey needs each order to know WHERE to send buyer-facing
replies (payment QR, ready notice, bill) independently of the shop's own
linked chat, and the shop-owner workflow needs a "ready" step between
confirmed and delivered.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("buyer_channel", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("buyer_chat_id", sa.String(), nullable=True))
    # Add the 'ready' value to the Postgres enum. ADD VALUE is not
    # transactional, so it must not share the migration's transaction —
    # autocommit_block() is Alembic's supported way to step outside it
    # (commits, runs in autocommit, reopens the transaction). Calling
    # connection.execution_options(isolation_level=...) directly fails on
    # SQLAlchemy 2.x once the batch_alter_table above has opened a
    # transaction, which is exactly what broke a fresh Postgres deploy.
    # SQLite (tests/dev) stores the value inline — nothing to alter there.
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            conn.execute(
                sa.text("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'ready'")
            )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("buyer_chat_id")
        batch_op.drop_column("buyer_channel")
