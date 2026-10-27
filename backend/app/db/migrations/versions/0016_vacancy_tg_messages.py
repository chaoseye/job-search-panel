"""cover-letter messages of the bot per vacancy (buttons ↔ panel)

Revision ID: 0016_vacancy_tg_messages
Revises: 0015_channel_post_lang
Create Date: 2026-09-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_vacancy_tg_messages"
down_revision = "0015_channel_post_lang"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vacancy_tg_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="active"),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_vacancy_tg_messages_vacancy", "vacancy_tg_messages", ["vacancy_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_vacancy_tg_messages_vacancy", table_name="vacancy_tg_messages")
    op.drop_table("vacancy_tg_messages")
