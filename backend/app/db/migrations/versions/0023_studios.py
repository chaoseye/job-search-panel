"""studios: development and design studios, letters to them through the Claude packet exchange

Revision ID: 0023_studios
Revises: 0022_drop_branded
Create Date: 2026-09-24

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_studios"
down_revision = "0022_drop_branded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studios",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=200), nullable=True, unique=True),
        sa.Column("site_url", sa.String(length=500), nullable=True),
        sa.Column("hh_employer_id", sa.String(length=20), nullable=True, unique=True),
        sa.Column("area", sa.String(length=200), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="unknown"),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=True),
        sa.Column("letter_kind", sa.String(length=12), nullable=False, server_default="application"),
        sa.Column("open_vacancies", sa.Integer(), nullable=True),
        sa.Column("services_json", sa.Text(), nullable=True),
        sa.Column("emails_json", sa.Text(), nullable=True),
        sa.Column("telegram", sa.String(length=100), nullable=True),
        sa.Column("contact_url", sa.String(length=500), nullable=True),
        sa.Column("contacts_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contacts_error", sa.Text(), nullable=True),
        sa.Column("fit_score", sa.Integer(), nullable=True),
        sa.Column("fit_notes_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="new"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_to", sa.String(length=200), nullable=True),
        sa.Column("sent_via", sa.String(length=12), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_subject", sa.String(length=300), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("site_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_studios_status", "studios", ["status", "fit_score"])
    op.create_table(
        "studio_letter_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("studio_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_studio_letter_requests_studio", "studio_letter_requests", ["studio_id", "status"])
    op.create_table(
        "studio_letters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("studio_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("needs_clarification_json", sa.Text(), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index("ix_studio_letters_studio", "studio_letters", ["studio_id", "is_current"])


def downgrade() -> None:
    op.drop_index("ix_studio_letters_studio", table_name="studio_letters")
    op.drop_table("studio_letters")
    op.drop_index("ix_studio_letter_requests_studio", table_name="studio_letter_requests")
    op.drop_table("studio_letter_requests")
    op.drop_index("ix_studios_status", table_name="studios")
    op.drop_table("studios")
