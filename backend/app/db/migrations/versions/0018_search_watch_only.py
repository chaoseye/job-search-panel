"""watch-only searches: new vacancies go to the bot as links, without evaluation

Revision ID: 0018_search_watch_only
Revises: 0017_company_sites
Create Date: 2026-09-17

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_search_watch_only"
down_revision = "0017_company_sites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("saved_searches") as batch:
        batch.add_column(sa.Column("watch_only", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("saved_searches") as batch:
        batch.drop_column("watch_only")
