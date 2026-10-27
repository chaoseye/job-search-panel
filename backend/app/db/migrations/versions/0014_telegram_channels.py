"""Telegram channels with vacancies: channels and collected posts

Revision ID: 0014_telegram_channels
Revises: 0013_questions_duplicates
Create Date: 2026-09-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_telegram_channels"
down_revision = "0013_questions_duplicates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="vacancies"),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("last_message_id", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_new", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("posts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "channel_posts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("links_json", sa.Text(), nullable=True),
        sa.Column("is_vacancy", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="new"),
        sa.Column("vacancy_id", sa.Integer(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_channel_posts_channel_message", "channel_posts", ["channel_id", "message_id"], unique=True)
    op.create_index("ix_channel_posts_status_posted", "channel_posts", ["status", "posted_at"])


def downgrade() -> None:
    op.drop_index("ix_channel_posts_status_posted", table_name="channel_posts")
    op.drop_index("ix_channel_posts_channel_message", table_name="channel_posts")
    op.drop_table("channel_posts")
    op.drop_table("telegram_channels")
