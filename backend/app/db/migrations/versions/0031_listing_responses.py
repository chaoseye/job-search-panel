"""vacancy listing index: the responses counter joins the covering index (the responses cap of «Вакансии»)

The filter «откликов не больше N» checks `responses_count` in the page of ids and in every counter of the tabs and
the menu, like the freshness filter checks the city (0030). With the counter in `ix_vacancies_listing` those queries
stay on the index; the sort «Меньше откликов» gets it too.

Revision ID: 0031_listing_responses
Revises: 0030_listing_area
Create Date: 2026-11-07

"""

from __future__ import annotations

from alembic import op

revision = "0031_listing_responses"
down_revision = "0030_listing_area"
branch_labels = None
depends_on = None

# 0030_listing_area.LISTING, then the responses counter
BEFORE = [
    "pipeline_status",
    "hidden",
    "snoozed_until",
    "seen_by_user_at",
    "score",
    "prescore",
    "published_at",
    "area_name",
]
LISTING = [*BEFORE, "responses_count"]


def upgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", LISTING, unique=False)


def downgrade() -> None:
    op.drop_index("ix_vacancies_listing", table_name="vacancies")
    op.create_index("ix_vacancies_listing", "vacancies", BEFORE, unique=False)
