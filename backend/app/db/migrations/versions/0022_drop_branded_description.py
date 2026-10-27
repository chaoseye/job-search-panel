"""stored vacancy JSON without `branded_description` (the employer's styled page the panel never reads)

Revision ID: 0022_drop_branded
Revises: 0021_vacancy_layout
Create Date: 2026-09-24

The field is most of `vacancies.raw_full_json`. New cards are stored without it
(hh.vacancy.storable_full); this removes it from the stored ones. The freed pages go back to the file at the next
VACUUM of the daily maintenance (services/maintenance.py).
"""

from __future__ import annotations

from alembic import op

revision = "0022_drop_branded"
down_revision = "0021_vacancy_layout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # json_valid: a row with broken JSON (never written by the panel, but a hand-edited base) is left as it is instead
    # of failing the migration and the start of the panel
    op.execute(
        "UPDATE vacancies SET raw_full_json = json_remove(raw_full_json, '$.branded_description') "
        "WHERE raw_full_json LIKE '%\"branded_description\"%' AND json_valid(raw_full_json)"
    )


def downgrade() -> None:
    pass  # the removed HTML was never used and cannot be restored
