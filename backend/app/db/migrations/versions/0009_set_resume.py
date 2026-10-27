"""a selection-criteria set may use its own resume (profile_set<id>.md for Claude)

Revision ID: 0009_set_resume
Revises: 0008_selection_profiles
Create Date: 2026-09-12

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_set_resume"
down_revision = "0008_selection_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("selection_profiles") as batch:
        batch.add_column(sa.Column("resume_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("selection_profiles") as batch:
        batch.drop_column("resume_id")
