"""studio letters in two styles: business and simple, the user picks one

Revision ID: 0025_studio_letter_style
Revises: 0024_studio_sites
Create Date: 2026-09-24

The letters written before are business ones (the only style there was).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_studio_letter_style"
down_revision = "0024_studio_sites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("studio_letters") as batch:
        batch.add_column(sa.Column("style", sa.String(length=10), nullable=False, server_default="business"))


def downgrade() -> None:
    with op.batch_alter_table("studio_letters") as batch:
        batch.drop_column("style")
