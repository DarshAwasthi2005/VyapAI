"""payments: link each confirmed payment to its Sales ledger row

Payment confirmation books a Sales entry; this column records which ledger
transaction_group that was, so posting is exactly idempotent and payments
confirmed before the link existed can be backfilled.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch_op:
        batch_op.add_column(
            sa.Column("ledger_transaction_group", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch_op:
        batch_op.drop_column("ledger_transaction_group")
