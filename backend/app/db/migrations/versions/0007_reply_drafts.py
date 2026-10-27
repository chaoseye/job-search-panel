"""reply drafts through the Claude packet exchange (M6)

Revision ID: 0007_reply_drafts
Revises: 0006_sync_notify
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_reply_drafts"
down_revision = "0006_sync_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("negotiation_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("user_intent", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(length=30), nullable=True),
        sa.Column("needs_clarification_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reply_drafts_negotiation", "reply_drafts", ["negotiation_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_reply_drafts_negotiation", table_name="reply_drafts")
    op.drop_table("reply_drafts")
