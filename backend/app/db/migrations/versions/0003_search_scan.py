"""saved_searches, vacancies, hidden_employers (M2)

Revision ID: 0003_search_scan
Revises: 0002_profile
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_search_scan"
down_revision = "0002_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="search"),
        sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("interval_min", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_found", sa.Integer(), nullable=True),
        sa.Column("last_new", sa.Integer(), nullable=True),
        sa.Column("last_requests", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_warning", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "vacancies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hh_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column("name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("employer_id", sa.String(length=32), nullable=True),
        sa.Column("employer_name", sa.String(length=300), nullable=True),
        sa.Column("employer_accredited_it", sa.Boolean(), nullable=True),
        sa.Column("employer_trusted", sa.Boolean(), nullable=True),
        sa.Column("area_id", sa.String(length=16), nullable=True),
        sa.Column("area_name", sa.String(length=200), nullable=True),
        sa.Column("salary_from", sa.Integer(), nullable=True),
        sa.Column("salary_to", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(length=8), nullable=True),
        sa.Column("salary_gross", sa.Boolean(), nullable=True),
        sa.Column("salary_net_estimate", sa.Integer(), nullable=True),
        sa.Column("salary_net_rur", sa.Integer(), nullable=True),
        sa.Column("experience_id", sa.String(length=32), nullable=True),
        sa.Column("experience_name", sa.String(length=100), nullable=True),
        sa.Column("employment_form_id", sa.String(length=32), nullable=True),
        sa.Column("employment_form_name", sa.String(length=100), nullable=True),
        sa.Column("work_format_ids_json", sa.Text(), nullable=True),
        sa.Column("work_format_names_json", sa.Text(), nullable=True),
        sa.Column("schedule_id", sa.String(length=32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("type_id", sa.String(length=16), nullable=True),
        sa.Column("response_letter_required", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("has_test", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("test_required", sa.Boolean(), nullable=True),
        sa.Column("response_url", sa.Text(), nullable=True),
        sa.Column("alternate_url", sa.Text(), nullable=True),
        sa.Column("apply_alternate_url", sa.Text(), nullable=True),
        sa.Column("key_skills_json", sa.Text(), nullable=True),
        sa.Column("professional_roles_json", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("raw_short_json", sa.Text(), nullable=True),
        sa.Column("raw_full_json", sa.Text(), nullable=True),
        sa.Column("full_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("full_fetch_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relations_json", sa.Text(), nullable=True),
        sa.Column("responses_count", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_search_ids", sa.Text(), nullable=False, server_default=","),
        sa.Column("pipeline_status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("pipeline_reason", sa.Text(), nullable=True),
        sa.Column("rules_passed", sa.Boolean(), nullable=True),
        sa.Column("rules_reasons_json", sa.Text(), nullable=True),
        sa.Column("rules_flags_json", sa.Text(), nullable=True),
        sa.Column("prescore", sa.Integer(), nullable=True),
        sa.Column("prescore_notes_json", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("seen_by_user_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_override", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_vacancies_status_first_seen", "vacancies", ["pipeline_status", "first_seen_at"])
    op.create_index("ix_vacancies_employer", "vacancies", ["employer_id"])
    op.create_index("ix_vacancies_published", "vacancies", ["published_at"])
    op.create_table(
        "hidden_employers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employer_id", sa.String(length=32), nullable=True, unique=True),
        sa.Column("employer_name", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hidden_employers")
    op.drop_index("ix_vacancies_published", table_name="vacancies")
    op.drop_index("ix_vacancies_employer", table_name="vacancies")
    op.drop_index("ix_vacancies_status_first_seen", table_name="vacancies")
    op.drop_table("vacancies")
    op.drop_table("saved_searches")
