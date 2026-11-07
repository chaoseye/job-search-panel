"""vacancy listing index: the city joins the covering index (the freshness filter of «Вакансии»)

The filter «другие города: до N дней» hides on «Вакансии» the vacancies outside the cities of the questionnaire
published more than N days ago. It is on by default, so the page of ids and every counter of the tabs and the menu
check `area_name` next to `published_at`. Without the city in `ix_vacancies_listing` each of those queries would read
every vacancy row (0021 made them index-only); with it they stay on the index.

Revision ID: 0030_listing_area
Revises: 0029_tender_answers
Create Date: 2026-11-07

"""

from __future__ import annotations

from alembic import op

revision = "0030_listing_area"
down_revision = "0029_tender_answers"
branch_labels = None
depends_on = None

# 0021_vacancy_layout.LISTING, then the city
BEFORE = ["pipeline_status", "hidden", "snoozed_until", "seen_by_user_at", "score", "prescore", "published_at"]
LISTING = [*BEFORE, "area_name"]


def upgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", LISTING, unique=False)


def downgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", BEFORE, unique=False)
