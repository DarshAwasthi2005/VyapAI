"""merchant email identity (email login)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("email", sa.String(), nullable=True))
    op.create_unique_constraint("uq_merchants_email", "merchants", ["email"])


def downgrade() -> None:
    op.drop_constraint("uq_merchants_email", "merchants", type_="unique")
    op.drop_column("merchants", "email")
