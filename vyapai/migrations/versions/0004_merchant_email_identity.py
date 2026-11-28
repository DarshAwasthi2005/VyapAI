"""merchant email identity (email login)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("merchants") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(), nullable=True))
        batch_op.create_unique_constraint("uq_merchants_email", ["email"])


def downgrade() -> None:
    with op.batch_alter_table("merchants") as batch_op:
        batch_op.drop_constraint("uq_merchants_email", type_="unique")
        batch_op.drop_column("email")
