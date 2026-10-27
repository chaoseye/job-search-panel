"""sign-in from a browser by a one-time link of the bot: the links (/open) and the browser sessions

Only SHA-256 of the link token and of the session cookie are stored; the token itself is in the owner's Telegram chat,
the cookie in the browser.

Revision ID: 0028_web_login
Revises: 0027_search_ids_not_reused
Create Date: 2026-10-23

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_web_login"
down_revision = "0027_search_ids_not_reused"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_login_tokens_hash", "login_tokens", ["token_hash"], unique=True)
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("login_token_id", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_web_sessions_hash", "web_sessions", ["session_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_web_sessions_hash", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_index("ix_login_tokens_hash", table_name="login_tokens")
    op.drop_table("login_tokens")
