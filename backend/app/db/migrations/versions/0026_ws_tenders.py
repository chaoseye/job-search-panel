"""tenders of workspace.ru: the watched list, the answers written by Claude

Revision ID: 0026_ws_tenders
Revises: 0025_studio_letter_style
Create Date: 2026-09-24

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_ws_tenders"
down_revision = "0025_studio_letter_style"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ws_tenders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ws_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("service", sa.String(length=300), nullable=True),
        sa.Column("conditions_json", sa.Text(), nullable=True),
        sa.Column("budget", sa.String(length=100), nullable=True),
        sa.Column("budget_max", sa.Integer(), nullable=True),
        sa.Column("published_on", sa.String(length=10), nullable=True),
        sa.Column("deadline_on", sa.String(length=10), nullable=True),
        sa.Column("state", sa.String(length=10), nullable=False, server_default="open"),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("organizer", sa.String(length=100), nullable=True),
        sa.Column("participants", sa.Integer(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=True),
        sa.Column("fit_notes_json", sa.Text(), nullable=True),
        sa.Column("relevant", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="new"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_error", sa.Text(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ws_tenders_status", "ws_tenders", ["status", "fit_score"])
    op.create_table(
        "ws_tender_reply_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tender_id", sa.Integer(), nullable=False),
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ws_tender_reply_requests_tender", "ws_tender_reply_requests", ["tender_id", "status"])
    op.create_table(
        "ws_tender_replies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tender_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("needs_clarification_json", sa.Text(), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ix_ws_tender_replies_tender", "ws_tender_replies", ["tender_id", "is_current"])


def downgrade() -> None:
    op.drop_index("ix_ws_tender_replies_tender", table_name="ws_tender_replies")
    op.drop_table("ws_tender_replies")
    op.drop_index("ix_ws_tender_reply_requests_tender", table_name="ws_tender_reply_requests")
    op.drop_table("ws_tender_reply_requests")
    op.drop_index("ix_ws_tenders_status", table_name="ws_tenders")
    op.drop_table("ws_tenders")
