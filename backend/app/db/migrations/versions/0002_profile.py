"""resumes and candidate_profile (M1)

Revision ID: 0002_profile
Revises: 0001_initial
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_profile"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("hh_resume_id", sa.String(length=64), nullable=True, unique=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("file_name", sa.String(length=300), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("structured_json", sa.Text(), nullable=True),
        sa.Column("text_extracted", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("hh_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("can_publish_or_update", sa.Boolean(), nullable=True),
        sa.Column("next_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_resumes_source_primary", "resumes", ["source", "is_primary"])
    op.create_table(
        "candidate_profile",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=True),
        sa.Column("file_resume_id", sa.Integer(), nullable=True),
        sa.Column("preferences_json", sa.Text(), nullable=False),
        sa.Column("profile_md", sa.Text(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_candidate_profile_active", "candidate_profile", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_candidate_profile_active", table_name="candidate_profile")
    op.drop_table("candidate_profile")
    op.drop_index("ix_resumes_source_primary", table_name="resumes")
    op.drop_table("resumes")
