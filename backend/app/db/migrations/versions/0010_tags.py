"""vacancy tags for analytics: rule tags on vacancies, Claude tags on evaluations

Revision ID: 0010_tags
Revises: 0009_set_resume
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_tags"
down_revision = "0009_set_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("tags_json", sa.Text(), nullable=True))
    with op.batch_alter_table("vacancy_evaluations") as batch:
        batch.add_column(sa.Column("tags_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vacancy_evaluations") as batch:
        batch.drop_column("tags_json")
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("tags_json")
