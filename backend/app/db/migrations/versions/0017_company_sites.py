"""company career sites: sites and the vacancies seen on them

Revision ID: 0017_company_sites
Revises: 0016_vacancy_tg_messages
Create Date: 2026-09-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_company_sites"
down_revision = "0016_vacancy_tg_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_sites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(length=40), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notify", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_new", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("first_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vacancies_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "site_vacancies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("site_key", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("work_format", sa.String(length=100), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_match", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="new"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_site_vacancies_site_external", "site_vacancies", ["site_key", "external_id"], unique=True)
    op.create_index("ix_site_vacancies_status", "site_vacancies", ["status", "is_match"])


def downgrade() -> None:
    op.drop_index("ix_site_vacancies_status", table_name="site_vacancies")
    op.drop_index("ix_site_vacancies_site_external", table_name="site_vacancies")
    op.drop_table("site_vacancies")
    op.drop_table("company_sites")
