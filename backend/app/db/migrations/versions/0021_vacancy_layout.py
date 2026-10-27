"""vacancy table layout: the large text columns go last, covering indexes for the counters and the statistics

A vacancy row is ~46 KB, 37 KB of it `raw_full_json`. SQLite stores a row's columns in table order and spills the
tail into a chain of overflow pages, so every column placed after the payload (status, scores, dates, flags — added
by later migrations) was read by walking ~10 overflow pages per row: filtering or sorting 10 000 vacancies read
~400 MB (target: statistics ≤ 500 ms; it took 2–4 s, the vacancy list «все» 1.7 s). The table is rebuilt with the
four large columns at the end — the small ones fit into the row's first page. Two covering indexes let the vacancy
tabs with their counters (every page of the panel shows them) and the statistics by `first_seen_at` skip the table.

Revision ID: 0021_vacancy_layout
Revises: 0020_claude_attempts
Create Date: 2026-09-23

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_vacancy_layout"
down_revision = "0020_claude_attempts"
branch_labels = None
depends_on = None

LARGE = ("description_html", "description_text", "raw_short_json", "raw_full_json")
# the filters of the vacancy tabs and the counters, then the default sort keys: the page of ids and every counter
# come from the index alone (select id … order by coalesce(score, prescore) … limit 50: 500 → 6 ms on 10 000)
LISTING = ["pipeline_status", "hidden", "snoozed_until", "seen_by_user_at", "score", "prescore", "published_at"]


def upgrade() -> None:
    names = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("vacancies")]
    order = [n for n in names if n not in LARGE] + [n for n in LARGE if n in names]
    # recreate="always" copies the rows into a new table with this column order (INSERT … SELECT by name),
    # keeps the primary key, the unique hh_id and the indexes
    with op.batch_alter_table("vacancies", recreate="always", partial_reordering=[tuple(order)]) as batch:
        batch.create_index("ix_vacancies_listing", LISTING, unique=False)
        batch.create_index(
            "ix_vacancies_first_seen", ["first_seen_at", "rules_passed", "pipeline_status"], unique=False
        )


def downgrade() -> None:
    # the column order is physical only: 0020 has the same columns, nothing to move back
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_index("ix_vacancies_first_seen")
        batch.drop_index("ix_vacancies_listing")
