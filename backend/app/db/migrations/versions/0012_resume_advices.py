"""resume advice requests through the Claude packet exchange

Revision ID: 0012_resume_advices
Revises: 0011_interview_preps
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_resume_advices"
down_revision = "0011_interview_preps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_advices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("segment_by", sa.String(length=20), nullable=True),
        sa.Column("segment_key", sa.String(length=200), nullable=True),
        sa.Column("segment_label", sa.String(length=200), nullable=False, server_default="все вакансии"),
        sa.Column("user_notes", sa.Text(), nullable=True),
        sa.Column("demand_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("resume_advices")
