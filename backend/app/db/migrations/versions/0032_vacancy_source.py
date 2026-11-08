"""vacancy source: the vacancies of hirehi.ru next to those of hh.ru

HireHi becomes a second source of «Вакансии» with the same pipeline (rules,
pre-score, packets for Claude, letters, the bot, the statistics). A vacancy now says where it lives — `source` (`hh` |
`hirehi`) and its number there (`source_id`, the HireHi job id); `hh_id` stays the hh.ru number and becomes optional:
a HireHi vacancy has none, so nothing that calls the hh.ru API by `hh_id` can touch it. One vacancy per number of a
source (`ux_vacancies_source_id`; hh rows keep NULL there, the unique `hh_id` guards them as before).

The table is rebuilt with the four large columns last again (0021): `add_column` would put the new columns after
them, and SQLite would read `source` of every row through the overflow pages.

Revision ID: 0032_vacancy_source
Revises: 0031_listing_responses
Create Date: 2026-11-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_vacancy_source"
down_revision = "0031_listing_responses"
branch_labels = None
depends_on = None

LARGE = ("description_html", "description_text", "raw_short_json", "raw_full_json")
NEW = ("source", "source_id")


def upgrade() -> None:
    names = [c["name"] for c in sa.inspect(op.get_bind()).get_columns("vacancies")]
    order = [n for n in names if n not in LARGE] + list(NEW) + [n for n in LARGE if n in names]
    # recreate="always": INSERT … SELECT into a new table with this column order; the primary key, the unique hh_id
    # and the indexes are kept
    with op.batch_alter_table("vacancies", recreate="always", partial_reordering=[tuple(order)]) as batch:
        batch.add_column(sa.Column("source", sa.String(length=16), nullable=False, server_default="hh"))
        batch.add_column(sa.Column("source_id", sa.String(length=64), nullable=True))
        batch.alter_column("hh_id", existing_type=sa.String(length=32), nullable=True)
        batch.create_index("ux_vacancies_source_id", ["source", "source_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    others = bind.execute(sa.text("SELECT count(*) FROM vacancies WHERE source <> 'hh' OR hh_id IS NULL")).scalar()
    if others:
        # 0031 has no place for them: hh_id NOT NULL. Dropping them would also orphan their letters, evaluations and
        # responses — the way back is the copy of the base made before the update (remote deploy: predeploy_*.db)
        raise RuntimeError(
            f"0032 → 0031: в базе {others} вакансий не с hh.ru (HireHi); откат их потерял бы. "
            "Восстановите копию базы, сделанную перед обновлением (predeploy_*.db)"
        )
    with op.batch_alter_table("vacancies", recreate="always") as batch:
        batch.drop_index("ux_vacancies_source_id")
        batch.drop_column("source_id")
        batch.drop_column("source")
        batch.alter_column("hh_id", existing_type=sa.String(length=32), nullable=False)
