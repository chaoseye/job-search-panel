"""negotiations mirror, messages, notifications queue, daily counters (M5)

Revision ID: 0006_sync_notify
Revises: 0005_applications
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_sync_notify"
down_revision = "0005_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "negotiations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("hh_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("vacancy_hh_id", sa.String(length=32), nullable=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("resume_hh_id", sa.String(length=64), nullable=True),
        sa.Column("state_id", sa.String(length=20), nullable=True),
        sa.Column("state_name", sa.String(length=100), nullable=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("viewed_by_opponent", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("has_updates", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("messaging_status", sa.String(length=40), nullable=True),
        sa.Column("chat_id", sa.String(length=32), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("hh_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hh_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_reaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_employer_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("messages_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_negotiations_state_updated", "negotiations", ["state_id", "hh_updated_at"])
    op.create_index("ix_negotiations_vacancy", "negotiations", ["vacancy_id"])
    op.create_index("ix_negotiations_application", "negotiations", ["application_id"])

    op.create_table(
        "negotiation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("negotiation_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("from_state", sa.String(length=20), nullable=True),
        sa.Column("to_state", sa.String(length=20), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_negotiation_events_neg", "negotiation_events", ["negotiation_id", "occurred_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("negotiation_id", sa.Integer(), nullable=False),
        sa.Column("hh_message_id", sa.String(length=64), nullable=False),
        sa.Column("author_type", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("author_name", sa.String(length=300), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("hh_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_by_me", sa.Boolean(), nullable=True),
        sa.Column("viewed_by_opponent", sa.Boolean(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="negotiation"),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_neg_hh", "messages", ["negotiation_id", "hh_message_id"], unique=True)

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=15), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("digest_key", sa.String(length=80), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notifications_status", "notifications", ["status", "channel"])
    op.create_index("ix_notifications_digest", "notifications", ["digest_key"])
    op.create_index("ix_notifications_created", "notifications", ["created_at"])

    op.create_table(
        "daily_counters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.String(length=10), nullable=False, unique=True),
        sa.Column("applies_api", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applies_assist_confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applies_external", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hh_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batches_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("letters_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("daily_counters")
    op.drop_index("ix_notifications_created", table_name="notifications")
    op.drop_index("ix_notifications_digest", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_messages_neg_hh", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_negotiation_events_neg", table_name="negotiation_events")
    op.drop_table("negotiation_events")
    op.drop_index("ix_negotiations_application", table_name="negotiations")
    op.drop_index("ix_negotiations_vacancy", table_name="negotiations")
    op.drop_index("ix_negotiations_state_updated", table_name="negotiations")
    op.drop_table("negotiations")
