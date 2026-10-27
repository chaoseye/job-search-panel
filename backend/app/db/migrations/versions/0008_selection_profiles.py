"""named selection criteria sets bound to searches and vacancies

Revision ID: 0008_selection_profiles
Revises: 0007_reply_drafts
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_selection_profiles"
down_revision = "0007_reply_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "selection_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("saved_searches") as batch:
        batch.add_column(sa.Column("selection_profile_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("selection_profile_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("selection_profile_id")
    with op.batch_alter_table("saved_searches") as batch:
        batch.drop_column("selection_profile_id")
    op.drop_table("selection_profiles")
