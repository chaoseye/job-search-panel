"""applications table, consent and apply pause in system_state (M4)

Revision ID: 0005_applications
Revises: 0004_exchange
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_applications"
down_revision = "0004_exchange"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("system_state") as batch:
        batch.add_column(sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("apply_paused_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("apply_paused_reason", sa.String(length=200), nullable=True))
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("resume_hh_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("cover_letter_id", sa.Integer(), nullable=True),
        sa.Column("letter_text", sa.Text(), nullable=True),
        sa.Column("prev_status", sa.String(length=20), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hh_negotiation_id", sa.String(length=64), nullable=True),
        sa.Column("response_url", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=50), nullable=True),
        sa.Column("error_value", sa.String(length=100), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_by_user", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_applications_status_sent", "applications", ["status", "sent_at"])
    op.create_index("ix_applications_vacancy", "applications", ["vacancy_id"])


def downgrade() -> None:
    op.drop_index("ix_applications_vacancy", table_name="applications")
    op.drop_index("ix_applications_status_sent", table_name="applications")
    op.drop_table("applications")
    with op.batch_alter_table("system_state") as batch:
        batch.drop_column("apply_paused_reason")
        batch.drop_column("apply_paused_until")
        batch.drop_column("consent_accepted_at")
