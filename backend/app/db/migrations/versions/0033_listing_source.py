"""vacancy listing index: the site of the vacancy joins the covering index (the filter «Сайт» of «Вакансии»)

The filter «Сайт» (after hirehi.ru became the second source) checks `source` in the page of ids and in its
counts. With the site in `ix_vacancies_listing` those queries stay on the index, like the city (0030) and the
responses counter (0031). The condition itself keeps off `ux_vacancies_source_id` (services/vacancies.source_condition):
with two sites over the whole table that index would make the page of a hundred candidates read every row of hh.ru.

Revision ID: 0033_listing_source
Revises: 0032_vacancy_source
Create Date: 2026-11-08

"""

from __future__ import annotations

from alembic import op

revision = "0033_listing_source"
down_revision = "0032_vacancy_source"
branch_labels = None
depends_on = None

# 0031_listing_responses.LISTING (0032 rebuilt the table and kept it), then the site
BEFORE = [
    "pipeline_status",
    "hidden",
    "snoozed_until",
    "seen_by_user_at",
    "score",
    "prescore",
    "published_at",
    "area_name",
    "responses_count",
]
LISTING = [*BEFORE, "source"]


def upgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", LISTING, unique=False)


def downgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", BEFORE, unique=False)
