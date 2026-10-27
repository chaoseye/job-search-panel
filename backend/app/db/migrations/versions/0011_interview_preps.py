"""interview preparation requests through the Claude packet exchange

Revision ID: 0011_interview_preps
Revises: 0010_tags
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_interview_preps"
down_revision = "0010_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_preps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("negotiation_id", sa.Integer(), nullable=True),
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_interview_preps_application", "interview_preps", ["application_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_interview_preps_application", table_name="interview_preps")
    op.drop_table("interview_preps")
