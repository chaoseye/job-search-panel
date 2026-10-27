"""saved searches with AUTOINCREMENT: the id of a deleted search is never given out again

Vacancies keep the ids of the searches that found them (`vacancies.source_search_ids`, «,3,5,»). Without
AUTOINCREMENT SQLite gives a new row max(id) + 1, so after the search with the highest id is deleted the next new
search got its id and inherited its vacancies: the filter «по поиску», the statistics by search and the rebind of
vacancies to a new criteria set treated them as its own. The table is rebuilt with AUTOINCREMENT; the counter
starts past the highest id any vacancy still refers to (a search deleted before this migration included).

Revision ID: 0027_search_ids_not_reused
Revises: 0026_ws_tenders
Create Date: 2026-09-25

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_search_ids_not_reused"
down_revision = "0026_ws_tenders"
branch_labels = None
depends_on = None


def _highest_referenced_id(conn: sa.Connection) -> int:
    highest = 0
    for (ids,) in conn.execute(sa.text("SELECT source_search_ids FROM vacancies WHERE source_search_ids != ','")):
        for part in (ids or "").split(","):
            part = part.strip()
            if part.isdigit():
                highest = max(highest, int(part))
    return highest


def upgrade() -> None:
    with op.batch_alter_table("saved_searches", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    conn = op.get_bind()
    # copying the rows set the counter to the highest existing id; a deleted search that vacancies still name
    # may have had a higher one
    highest = _highest_referenced_id(conn)
    if highest:
        conn.execute(
            sa.text("UPDATE sqlite_sequence SET seq = :n WHERE name = 'saved_searches' AND seq < :n"), {"n": highest}
        )
        conn.execute(
            sa.text(
                "INSERT INTO sqlite_sequence (name, seq) SELECT 'saved_searches', :n "
                "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'saved_searches')"
            ),
            {"n": highest},
        )


def downgrade() -> None:
    with op.batch_alter_table("saved_searches", recreate="always", table_kwargs={"sqlite_autoincrement": False}):
        pass
    op.execute("DELETE FROM sqlite_sequence WHERE name = 'saved_searches'")
