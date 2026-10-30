"""real merchant auth + channel linking

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17

Adds:
- merchants.pin_hash / pin_salt (PBKDF2 material), telegram_chat_id,
  whatsapp_chat_id
- login_challenges (hashed OTP / pairing code, single-use, expiring)
- auth_sessions (digest-only bearer tokens)
- merchant_channel_preferences (per-role channel choice: otp / orders)
All money/audio-payload columns from 0001/0002 are untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("pin_hash", sa.String, nullable=True))
    op.add_column("merchants", sa.Column("pin_salt", sa.String, nullable=True))
    op.add_column("merchants", sa.Column("telegram_chat_id", sa.String, nullable=True))
    op.add_column("merchants", sa.Column("whatsapp_chat_id", sa.String, nullable=True))

    op.create_table(
        "login_challenges",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("purpose", sa.String, nullable=False),
        sa.Column("secret_hash", sa.String, nullable=False),
        sa.Column("delivery", sa.String, server_default="telegram", nullable=False),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer, server_default="5", nullable=False),
        sa.Column("consumed_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_login_challenges_merchant_id", "login_challenges", ["merchant_id"])
    op.create_index("ix_login_challenges_purpose", "login_challenges", ["purpose"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("token_hash", sa.String, nullable=False, unique=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"])
    op.create_index("ix_auth_sessions_merchant_id", "auth_sessions", ["merchant_id"])

    op.create_table(
        "merchant_channel_preferences",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("merchant_id", sa.String, sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("channel", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mcp_merchant_role", "merchant_channel_preferences", ["merchant_id", "role"])


def downgrade() -> None:
    op.drop_table("merchant_channel_preferences")
    op.drop_table("auth_sessions")
    op.drop_table("login_challenges")
    op.drop_column("merchants", "whatsapp_chat_id")
    op.drop_column("merchants", "telegram_chat_id")
    op.drop_column("merchants", "pin_salt")
    op.drop_column("merchants", "pin_hash")
