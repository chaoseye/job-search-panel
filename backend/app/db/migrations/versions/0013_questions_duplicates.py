"""employer questions at application (task answer_questions) and duplicate vacancies

Revision ID: 0013_questions_duplicates
Revises: 0012_resume_advices
Create Date: 2026-09-14

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_questions_duplicates"
down_revision = "0012_resume_advices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_answers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("questions_text", sa.Text(), nullable=False),
        sa.Column("questions_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_question_answers_vacancy", "question_answers", ["vacancy_id", "status"])
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("duplicate_of_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("duplicate_of_id")
    op.drop_index("ix_question_answers_vacancy", table_name="question_answers")
    op.drop_table("question_answers")
