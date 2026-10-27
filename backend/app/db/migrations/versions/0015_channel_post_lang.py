"""language of a channel post (ru | en | uk | other) for the language filter

Revision ID: 0015_channel_post_lang
Revises: 0014_telegram_channels
Create Date: 2026-09-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_channel_post_lang"
down_revision = "0014_telegram_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("channel_posts") as batch:
        batch.add_column(sa.Column("lang", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("channel_posts") as batch:
        batch.drop_column("lang")
