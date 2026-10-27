"""failed Claude attempts per vacancy: after three in a row the vacancy is parked instead of re-packed forever

Revision ID: 0020_claude_attempts
Revises: 0019_vacancy_place
Create Date: 2026-09-23

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_claude_attempts"
down_revision = "0019_vacancy_place"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("claude_attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("claude_attempts")
