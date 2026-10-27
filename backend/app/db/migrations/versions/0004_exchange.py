"""claude_batches, vacancy_evaluations, cover_letters, letter_revision_requests, vacancies.batch_id (M3)

Revision ID: 0004_exchange
Revises: 0003_search_scan
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_exchange"
down_revision = "0003_search_scan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("batch_id", sa.String(length=40), nullable=True))
    op.create_table(
        "claude_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(length=40), nullable=False, unique=True),
        sa.Column("packet_path", sa.Text(), nullable=False),
        sa.Column("result_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("tasks_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("results_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vacancy_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("matched_json", sa.Text(), nullable=True),
        sa.Column("gaps_json", sa.Text(), nullable=True),
        sa.Column("red_flags_json", sa.Text(), nullable=True),
        sa.Column("salary_fit", sa.String(length=10), nullable=True),
        sa.Column("seniority_fit", sa.String(length=10), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("user_feedback", sa.String(length=10), nullable=True),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vacancy_evaluations_vacancy", "vacancy_evaluations", ["vacancy_id", "is_current"])
    op.create_index("ix_vacancy_evaluations_batch", "vacancy_evaluations", ["vacancy_id", "batch_id"], unique=True)
    op.create_table(
        "cover_letters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("origin", sa.String(length=10), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("mentions_json", sa.Text(), nullable=True),
        sa.Column("needs_clarification_json", sa.Text(), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cover_letters_vacancy", "cover_letters", ["vacancy_id", "is_current"])
    op.create_table(
        "letter_revision_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("previous_letter_id", sa.Integer(), nullable=True),
        sa.Column("user_comment", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_letter_revision_vacancy", "letter_revision_requests", ["vacancy_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_letter_revision_vacancy", table_name="letter_revision_requests")
    op.drop_table("letter_revision_requests")
    op.drop_index("ix_cover_letters_vacancy", table_name="cover_letters")
    op.drop_table("cover_letters")
    op.drop_index("ix_vacancy_evaluations_batch", table_name="vacancy_evaluations")
    op.drop_index("ix_vacancy_evaluations_vacancy", table_name="vacancy_evaluations")
    op.drop_table("vacancy_evaluations")
    op.drop_table("claude_batches")
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("batch_id")
