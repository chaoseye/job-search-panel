"""tenders: the organizer's answer recorded by hand (answered or declined) and when it was recorded

Revision ID: 0029_tender_answers
Revises: 0028_web_login
Create Date: 2026-10-25

The statuses `replied` and `declined` need no change of the schema (the status is a string); `answered_at` keeps when
the answer was recorded: the board's «Ответ» column is ordered and the funnel counts by it. A downgrade returns the
answered tenders to `responded`, the status the code before knows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_tender_answers"
down_revision = "0028_web_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ws_tenders") as batch:
        batch.add_column(sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE ws_tenders SET status = 'responded' WHERE status IN ('replied', 'declined')")
    with op.batch_alter_table("ws_tenders") as batch:
        batch.drop_column("answered_at")
