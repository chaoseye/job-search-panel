"""ORM models of the panel."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utcnow


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class HHAuth(TimestampMixin, Base):
    """Single row (id=1) with the applicant's OAuth tokens."""

    __tablename__ = "hh_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hh_user_id: Mapped[str | None] = mapped_column(String(64))
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    obtained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # none | ok | expired | revoked
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")


class SystemState(TimestampMixin, Base):
    """Single row (id=1) with runtime state of the panel."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused_reason: Mapped[str | None] = mapped_column(String(50))
    captcha_url: Mapped[str | None] = mapped_column(Text)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # effective mode: api | assist
    apply_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="assist", server_default="assist")
    # user override: auto | api | assist
    apply_mode_override: Mapped[str] = mapped_column(String(10), nullable=False, default="auto", server_default="auto")
    capabilities_json: Mapped[str | None] = mapped_column(Text)
    probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # applying (M4)
    consent_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_paused_reason: Mapped[str | None] = mapped_column(String(200))


class Application(TimestampMixin, Base):
    """An application (отклик) made or planned by the panel; `external` rows come from sync (M5)."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_hh_id: Mapped[str | None] = mapped_column(String(64))
    # api | assist | external
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    # queued | sending | sent | failed | needs_manual | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    cover_letter_id: Mapped[int | None] = mapped_column(Integer)
    letter_text: Mapped[str | None] = mapped_column(Text)
    prev_status: Mapped[str | None] = mapped_column(String(20))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hh_negotiation_id: Mapped[str | None] = mapped_column(String(64))
    response_url: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(50))
    error_value: Mapped[str | None] = mapped_column(String(100))
    error_text: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    autopilot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    __table_args__ = (
        Index("ix_applications_status_sent", "status", "sent_at"),
        Index("ix_applications_vacancy", "vacancy_id"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running | ok | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    stats_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_job_runs_job_started", "job", "started_at"),)


class Resume(TimestampMixin, Base):
    """A resume source: imported from hh.ru (structured JSON) or uploaded as a file (plain text)."""

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # hh | file
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    hh_resume_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    title: Mapped[str | None] = mapped_column(String(300))
    file_path: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(300))
    raw_json: Mapped[str | None] = mapped_column(Text)
    structured_json: Mapped[str | None] = mapped_column(Text)
    text_extracted: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    hh_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    can_publish_or_update: Mapped[bool | None] = mapped_column(Boolean)
    next_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_resumes_source_primary", "source", "is_primary"),)


class CandidateProfile(TimestampMixin, Base):
    """A versioned snapshot of profile.md and its inputs."""

    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_id: Mapped[int | None] = mapped_column(Integer)
    file_resume_id: Mapped[int | None] = mapped_column(Integer)
    preferences_json: Mapped[str] = mapped_column(Text, nullable=False)
    profile_md: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    __table_args__ = (Index("ix_candidate_profile_active", "is_active"),)


class SavedSearch(TimestampMixin, Base):
    __tablename__ = "saved_searches"
    # AUTOINCREMENT: a deleted search's id is never given out again. Vacancies keep the ids of the searches that
    # found them (`source_search_ids`), and without it SQLite hands the highest deleted id to the next new search,
    # which would inherit those vacancies in the filter, the statistics and a criteria-set rebind (migration 0027)
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # search | similar_to_resume
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="search", server_default="search")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    interval_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_found: Mapped[int | None] = mapped_column(Integer)
    last_new: Mapped[int | None] = mapped_column(Integer)
    last_requests: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_warning: Mapped[str | None] = mapped_column(Text)
    # named selection criteria set (None = base questionnaire)
    selection_profile_id: Mapped[int | None] = mapped_column(Integer)
    # watch-only: new vacancies are stored as `watched` and go to the bot as links (event search_new) — no rules,
    # no prescore, no packets (tracking a role the questionnaire is not about, e.g. a second profession)
    watch_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class Vacancy(TimestampMixin, Base):
    __tablename__ = "vacancies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hh_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    employer_id: Mapped[str | None] = mapped_column(String(32))
    employer_name: Mapped[str | None] = mapped_column(String(300))
    employer_accredited_it: Mapped[bool | None] = mapped_column(Boolean)
    employer_trusted: Mapped[bool | None] = mapped_column(Boolean)
    area_id: Mapped[str | None] = mapped_column(String(16))
    area_name: Mapped[str | None] = mapped_column(String(200))
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_gross: Mapped[bool | None] = mapped_column(Boolean)
    salary_net_estimate: Mapped[int | None] = mapped_column(Integer)
    salary_net_rur: Mapped[int | None] = mapped_column(Integer)
    experience_id: Mapped[str | None] = mapped_column(String(32))
    experience_name: Mapped[str | None] = mapped_column(String(100))
    employment_form_id: Mapped[str | None] = mapped_column(String(32))
    employment_form_name: Mapped[str | None] = mapped_column(String(100))
    work_format_ids_json: Mapped[str | None] = mapped_column(Text)
    work_format_names_json: Mapped[str | None] = mapped_column(Text)
    schedule_id: Mapped[str | None] = mapped_column(String(32))
    # workplace from hh `address`: «улица, дом» and the metro stations (JSON list, the nearest first)
    address: Mapped[str | None] = mapped_column(String(300))
    metro_json: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    type_id: Mapped[str | None] = mapped_column(String(16))
    response_letter_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    has_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    test_required: Mapped[bool | None] = mapped_column(Boolean)
    response_url: Mapped[str | None] = mapped_column(Text)
    alternate_url: Mapped[str | None] = mapped_column(Text)
    apply_alternate_url: Mapped[str | None] = mapped_column(Text)
    key_skills_json: Mapped[str | None] = mapped_column(Text)
    professional_roles_json: Mapped[str | None] = mapped_column(Text)
    full_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    full_fetch_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    relations_json: Mapped[str | None] = mapped_column(Text)
    responses_count: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ",1,2," — comma-wrapped list of saved_search ids for LIKE lookups
    source_search_ids: Mapped[str] = mapped_column(Text, nullable=False, default=",", server_default=",")
    pipeline_status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", server_default="new")
    pipeline_reason: Mapped[str | None] = mapped_column(Text)
    rules_passed: Mapped[bool | None] = mapped_column(Boolean)
    rules_reasons_json: Mapped[str | None] = mapped_column(Text)
    rules_flags_json: Mapped[str | None] = mapped_column(Text)
    prescore: Mapped[int | None] = mapped_column(Integer)
    prescore_notes_json: Mapped[str | None] = mapped_column(Text)
    # filled by Claude evaluation (M3)
    score: Mapped[int | None] = mapped_column(Integer)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    seen_by_user_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    skip_reason: Mapped[str | None] = mapped_column(Text)
    # packet the vacancy is currently waiting in (awaiting_claude)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    # criteria set the vacancy was filtered/prescored with (from the search that found it; None = base)
    selection_profile_id: Mapped[int | None] = mapped_column(Integer)
    # rule tags for analytics: {"domain": [...], "tasks": [...], "stack": [...]} (services/tagging.py)
    tags_json: Mapped[str | None] = mapped_column(Text)
    # the earlier vacancy this one repeats (same employer and title): skipped as a duplicate (scanner.find_duplicate)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer)
    # Claude failures in a row on this vacancy (result with `error`, without an evaluation or no result at all); at
    # importer.MAX_CLAUDE_ATTEMPTS it is parked in needs_manual instead of being re-packed forever
    claude_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The large text columns stay LAST in the table (migration 0021): a row was ~46 KB (~10 KB since 0022 dropped
    # `branded_description` from raw_full_json) and SQLite reads every column stored after them through a chain of
    # overflow pages. `add_column` appends to the end, i.e. after these — a migration adding a column filtered or
    # sorted on must rebuild the table with them last again
    # (batch_alter_table(recreate="always", partial_reordering=...), see 0021_vacancy_layout).
    description_html: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    raw_short_json: Mapped[str | None] = mapped_column(Text)
    raw_full_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_vacancies_status_first_seen", "pipeline_status", "first_seen_at"),
        Index("ix_vacancies_employer", "employer_id"),
        Index("ix_vacancies_published", "published_at"),
        # covering indexes (migration 0021): the tab lists (ids of a page), the counters and the statistics by
        # first_seen_at never read the rows
        Index(
            "ix_vacancies_listing",
            "pipeline_status",
            "hidden",
            "snoozed_until",
            "seen_by_user_at",
            "score",
            "prescore",
            "published_at",
        ),
        Index("ix_vacancies_first_seen", "first_seen_at", "rules_passed", "pipeline_status"),
    )


class ClaudeBatch(TimestampMixin, Base):
    """A packet of tasks written to data/exchange/pending for the /hh-batch command."""

    __tablename__ = "claude_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    packet_path: Mapped[str] = mapped_column(Text, nullable=False)
    result_path: Mapped[str | None] = mapped_column(Text)
    # pending | imported | partial | rejected | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    tasks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    results_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    profile_version: Mapped[int | None] = mapped_column(Integer)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str | None] = mapped_column(Text)


class VacancyEvaluation(TimestampMixin, Base):
    __tablename__ = "vacancy_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_version: Mapped[int | None] = mapped_column(Integer)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    matched_json: Mapped[str | None] = mapped_column(Text)
    gaps_json: Mapped[str | None] = mapped_column(Text)
    red_flags_json: Mapped[str | None] = mapped_column(Text)
    salary_fit: Mapped[str | None] = mapped_column(String(10))
    seniority_fit: Mapped[str | None] = mapped_column(String(10))
    language: Mapped[str | None] = mapped_column(String(10))
    # ok | wrong | null
    user_feedback: Mapped[str | None] = mapped_column(String(10))
    user_comment: Mapped[str | None] = mapped_column(Text)
    # tags Claude returned with the evaluation (guidelines v4): {"domain": [...], "tasks": [...], "stack": [...]}
    tags_json: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    __table_args__ = (
        Index("ix_vacancy_evaluations_vacancy", "vacancy_id", "is_current"),
        Index("ix_vacancy_evaluations_batch", "vacancy_id", "batch_id", unique=True),
    )


class CoverLetter(TimestampMixin, Base):
    __tablename__ = "cover_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10))
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # claude | edited | template
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    mentions_json: Mapped[str | None] = mapped_column(Text)
    needs_clarification_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    __table_args__ = (Index("ix_cover_letters_vacancy", "vacancy_id", "is_current"),)


class LetterRevisionRequest(TimestampMixin, Base):
    __tablename__ = "letter_revision_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_letter_id: Mapped[int | None] = mapped_column(Integer)
    user_comment: Mapped[str] = mapped_column(Text, nullable=False)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (Index("ix_letter_revision_vacancy", "vacancy_id", "status"),)


class HiddenEmployer(Base):
    __tablename__ = "hidden_employers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employer_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    employer_name: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    area: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_audit_log_ts", "ts"),)


# ---- M5: sync, messages, notifications, counters ------------------------------


class Negotiation(TimestampMixin, Base):
    """Mirror of an hh.ru negotiation (отклик/приглашение) — filled by the sync job."""

    __tablename__ = "negotiations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hh_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    vacancy_hh_id: Mapped[str | None] = mapped_column(String(32))
    vacancy_id: Mapped[int | None] = mapped_column(Integer)
    application_id: Mapped[int | None] = mapped_column(Integer)
    resume_hh_id: Mapped[str | None] = mapped_column(String(64))
    # response | invitation | discard | hidden (negotiations_state dictionary)
    state_id: Mapped[str | None] = mapped_column(String(20))
    state_name: Mapped[str | None] = mapped_column(String(100))
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    viewed_by_opponent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    has_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    messaging_status: Mapped[str | None] = mapped_column(String(40))
    chat_id: Mapped[str | None] = mapped_column(String(32))
    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    url: Mapped[str | None] = mapped_column(Text)
    hh_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hh_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # derived timestamps for statistics
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_reaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_employer_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    messages_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_negotiations_state_updated", "state_id", "hh_updated_at"),
        Index("ix_negotiations_vacancy", "vacancy_id"),
        Index("ix_negotiations_application", "application_id"),
    )


class NegotiationEvent(Base):
    """History of a negotiation for reaction-time metrics and notifications."""

    __tablename__ = "negotiation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    negotiation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # created | viewed | state_changed | message
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str | None] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("ix_negotiation_events_neg", "negotiation_id", "occurred_at"),)


class Message(Base):
    """Incoming/outgoing chat messages of a negotiation (read-only mirror)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    negotiation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    hh_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # applicant | employer | bot | unknown
    author_type: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    author_name: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str | None] = mapped_column(Text)
    hh_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_by_me: Mapped[bool | None] = mapped_column(Boolean)
    viewed_by_opponent: Mapped[bool | None] = mapped_column(Boolean)
    # negotiation | chat
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="negotiation")
    raw_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("ix_messages_neg_hh", "negotiation_id", "hh_message_id", unique=True),)


class Notification(Base):
    """Notification queue: one `ui` row per event plus a `telegram` row when enabled."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ui | telegram
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # queued | sent | failed | suppressed
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    digest_key: Mapped[str | None] = mapped_column(String(80))
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_notifications_status", "status", "channel"),
        Index("ix_notifications_digest", "digest_key"),
        Index("ix_notifications_created", "created_at"),
    )


class DailyCounter(Base):
    """Per-day counters for limits and the dashboard. `date` is the local calendar day (YYYY-MM-DD)."""

    __tablename__ = "daily_counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)
    applies_api: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    applies_assist_confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    applies_external: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    hh_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    batches_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    letters_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ReplyDraft(TimestampMixin, Base):
    """Draft of a reply to an employer, written by Claude through the packet exchange (task `reply_draft`)."""

    __tablename__ = "reply_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    negotiation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_intent: Mapped[str | None] = mapped_column(Text)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    draft_text: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(30))
    needs_clarification_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_reply_drafts_negotiation", "negotiation_id", "status"),)


class InterviewPrep(TimestampMixin, Base):
    """Interview preparation sheet written by Claude through the packet exchange (task `interview_prep`)."""

    __tablename__ = "interview_preps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    negotiation_id: Mapped[int | None] = mapped_column(Integer)
    user_notes: Mapped[str | None] = mapped_column(Text)
    # created automatically when an invitation was recorded
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    # {"summary", "questions": [{"question", "answer"}], "ask_them": [], "risks": [], "prepare": []}
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_interview_preps_application", "application_id", "status"),)


class QuestionAnswers(TimestampMixin, Base):
    """Answers to the employer's questions in the response form, written by Claude (task `answer_questions`)."""

    __tablename__ = "question_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    questions_text: Mapped[str] = mapped_column(Text, nullable=False)
    questions_json: Mapped[str | None] = mapped_column(Text)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    # {"answers": [{"question", "answer", "needs_clarification": []}]}
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_question_answers_vacancy", "vacancy_id", "status"),)


class ResumeAdvice(TimestampMixin, Base):
    """Concrete resume edits proposed by Claude for a vacancy segment (task `resume_advice`)."""

    __tablename__ = "resume_advices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment_by: Mapped[str | None] = mapped_column(String(20))
    segment_key: Mapped[str | None] = mapped_column(String(200))
    segment_label: Mapped[str] = mapped_column(String(200), nullable=False, default="все вакансии")
    user_notes: Mapped[str | None] = mapped_column(Text)
    # what the segment demanded when the advice was requested (services/resume_advice.demand_snapshot)
    demand_json: Mapped[str | None] = mapped_column(Text)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    # {"summary", "edits": [{"section", "proposed", "reason"}], "add_skills": [], "soften": [], "questions": []}
    result_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class SelectionProfile(TimestampMixin, Base):
    """Named set of vacancy selection criteria (a subset of the questionnaire fields) bound to saved searches.

    The base questionnaire (`settings.preferences`) is the default set; each named set overrides only the
    rule/prescore fields (CRITERIA_FIELDS in services/preferences.py), the profile and letters stay shared.
    """

    __tablename__ = "selection_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    criteria_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # optional own resume (resumes.id): the set then gets profile_set<id>.md for Claude and its own skills
    resume_id: Mapped[int | None] = mapped_column(Integer)


# ---- Telegram channels with vacancies -------------------------------------------------


class TelegramChannel(TimestampMixin, Base):
    """A public Telegram channel the panel reads through its web preview (t.me/s/<username>)."""

    __tablename__ = "telegram_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # vacancies — only posts that look like a vacancy (services/channels.looks_like_vacancy); all — every post
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="vacancies", server_default="vacancies")
    # optional comma-separated words: a post counts as a vacancy only if it mentions one of them
    keywords: Mapped[str | None] = mapped_column(Text)
    # send new vacancy posts of this channel to Telegram (event channel_post)
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # newest message id already collected; None until the first sync (its posts are a backfill, not «new»)
    last_message_id: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_new: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ChannelPost(Base):
    """A post collected from a Telegram channel (text as shown in the preview, links, vacancy flag)."""

    __tablename__ = "channel_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # first line of the post (the vacancy title in most job channels)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html: Mapped[str | None] = mapped_column(Text)
    links_json: Mapped[str | None] = mapped_column(Text)
    is_vacancy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # detected language: ru | en | uk | other (services/channels.detect_language)
    lang: Mapped[str | None] = mapped_column(String(8))
    # new | seen | hidden
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="new", server_default="new")
    # panel vacancy when the post links an hh.ru vacancy that is (or was added) in the base
    vacancy_id: Mapped[int | None] = mapped_column(Integer)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_channel_posts_channel_message", "channel_id", "message_id", unique=True),
        Index("ix_channel_posts_status_posted", "status", "posted_at"),
    )


class VacancyTgMessage(Base):
    """A cover-letter message the bot sent for a vacancy (event `letter`), so the panel can edit or delete it when
    the vacancy is handled — from the bot buttons or from the panel."""

    __tablename__ = "vacancy_tg_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vacancy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    notification_id: Mapped[int | None] = mapped_column(Integer)
    # active (buttons shown) | resolve (handled in the panel: delete on the next tick) | resolved
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active", server_default="active")
    # sent | skipped | snoozed | hidden — what happened to the vacancy
    resolution: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_vacancy_tg_messages_vacancy", "vacancy_id", "status"),)


# ---- company career sites ---------------------------------------------------------------


class CompanySite(TimestampMixin, Base):
    """A company career site the panel reads with its adapter (services/company_sites.ADAPTERS, keyed by `key`)."""

    __tablename__ = "company_sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    # optional comma-separated words: a vacancy counts as a match only if its title mentions one of them
    keywords: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_new: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    # None until the first read: its vacancies are a backfill, not «new»
    first_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vacancies_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class SiteVacancy(Base):
    """A vacancy seen on a company site; `is_match` = the location rule (city or remote) and the title filters."""

    __tablename__ = "site_vacancies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_key: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    company: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(500))
    work_format: Mapped[str | None] = mapped_column(String(100))
    remote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    snippet: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # new | seen | hidden | gone (not on the site any more)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="new", server_default="new")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_site_vacancies_site_external", "site_key", "external_id", unique=True),
        Index("ix_site_vacancies_status", "status", "is_match"),
    )


# ---- studios: initiative letters to development and design studios ------------------------------------------------


class Studio(TimestampMixin, Base):
    """A development or design studio the user may write to. Sources: hh.ru employer search and the employers of the
    panel's vacancies (hh data may serve job search only — hh developer agreement §3.3, §4.5, §4.7 — so such a studio
    gets an initiative job application, never a sales offer: `letter_kind` is forced to «application»), the user's own
    list, Telegram channel posts. Contacts come from the studio's own site (services/studio_contacts.py)."""

    __tablename__ = "studios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    # host of the site without www — the dedupe key of studios that have a site
    domain: Mapped[str | None] = mapped_column(String(200), unique=True)
    site_url: Mapped[str | None] = mapped_column(String(500))
    hh_employer_id: Mapped[str | None] = mapped_column(String(20), unique=True)
    area: Mapped[str | None] = mapped_column(String(200))
    # dev | design | mixed | unknown
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown", server_default="unknown")
    # hh_search | hh_vacancies | list | channel (the first one; every source seen is in sources_json)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    sources_json: Mapped[str | None] = mapped_column(Text)
    # application (job search, the only kind for hh studios) | offer (services; only studios without hh data)
    letter_kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default="application", server_default="application"
    )
    open_vacancies: Mapped[int | None] = mapped_column(Integer)
    services_json: Mapped[str | None] = mapped_column(Text)
    emails_json: Mapped[str | None] = mapped_column(Text)
    telegram: Mapped[str | None] = mapped_column(String(100))
    contact_url: Mapped[str | None] = mapped_column(String(500))
    contacts_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contacts_error: Mapped[str | None] = mapped_column(Text)
    fit_score: Mapped[int | None] = mapped_column(Integer)
    fit_notes_json: Mapped[str | None] = mapped_column(Text)
    # new → ready | no_contacts → drafting → drafted → sent → replied | declined; skipped, hidden (user)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="new", server_default="new")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_to: Mapped[str | None] = mapped_column(String(200))
    sent_via: Mapped[str | None] = mapped_column(String(12))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_subject: Mapped[str | None] = mapped_column(String(300))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    site_summary: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_studios_status", "status", "fit_score"),)


class StudioLetterRequest(TimestampMixin, Base):
    """A letter for a studio asked from Claude (task `studio_letter`, key `studio-<id>` of this row)."""

    __tablename__ = "studio_letter_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    studio_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="application")
    user_notes: Mapped[str | None] = mapped_column(Text)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_studio_letter_requests_studio", "studio_id", "status"),)


class StudioLetter(Base):
    """A version of the letter to a studio: from Claude or edited by the user. Claude writes one draft per style
    (business and simple, the user picks); `is_current` marks the one picked — the
    latest version of the style the user worked with last."""

    __tablename__ = "studio_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    studio_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="application")
    # business | simple (services/studios.LETTER_STYLES)
    style: Mapped[str] = mapped_column(String(10), nullable=False, default="business", server_default="business")
    subject: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # claude | edited
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    needs_clarification_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_studio_letters_studio", "studio_id", "is_current"),)


class WsTender(TimestampMixin, Base):
    """A public tender of workspace.ru (services/ws_tenders.py): an organizer looks for a
    contractor of digital services; the user answers it on workspace.ru, the panel watches the list, scores the tender
    against the profile and keeps a draft of the answer written by Claude."""

    __tablename__ = "ws_tenders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # the number at the end of the tender's address (/tenders/<slug>-18968/)
    ws_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # the watched section the tender was listed in (web-development, crm, apps-development …); none — the common list
    category: Mapped[str | None] = mapped_column(String(40))
    # «Требуемая услуга» and the other conditions of the card
    service: Mapped[str | None] = mapped_column(String(300))
    conditions_json: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[str | None] = mapped_column(String(100))
    budget_max: Mapped[int | None] = mapped_column(Integer)
    # ISO dates (YYYY-MM-DD) as the site shows them
    published_on: Mapped[str | None] = mapped_column(String(10))
    deadline_on: Mapped[str | None] = mapped_column(String(10))
    # open | closed (the site's «Идет прием заявок» / «Завершён»)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default="open", server_default="open")
    city: Mapped[str | None] = mapped_column(String(100))
    organizer: Mapped[str | None] = mapped_column(String(100))
    participants: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int | None] = mapped_column(Integer)
    fit_score: Mapped[int | None] = mapped_column(Integer)
    fit_notes_json: Mapped[str | None] = mapped_column(Text)
    # suits the profile: a watched section or enough in common with the questionnaire, no stop word in the title
    relevant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # new → interesting → drafting → drafted → responded → replied | declined (the organizer's answer, recorded by
    # hand); skipped, hidden (the user)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="new", server_default="new")
    # the bot was told — or there was nothing to tell (the first read of an empty table)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail_error: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # when the organizer's answer (replied or declined) was recorded
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_ws_tenders_status", "status", "fit_score"),)


class WsTenderReplyRequest(TimestampMixin, Base):
    """An answer to a tender asked from Claude (task `tender_reply`, key `tender-<id>` of this row)."""

    __tablename__ = "ws_tender_reply_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tender_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_notes: Mapped[str | None] = mapped_column(Text)
    # queued | sent_in_batch | done | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    batch_id: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_ws_tender_reply_requests_tender", "tender_id", "status"),)


class WsTenderReply(Base):
    """A version of the answer to a tender: from Claude or edited by the user; the user posts it on workspace.ru."""

    __tablename__ = "ws_tender_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tender_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # claude | edited
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    needs_clarification_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_ws_tender_replies_tender", "tender_id", "is_current"),)


# ---- sign-in from a browser through the public address (services/web_login.py) ------------------------


class LoginToken(Base):
    """A one-time sign-in link the bot sent to the owner's chat (/open). Only the SHA-256 of the token is kept: the
    link itself lives in the chat. Used once (`used_at`), refused after `expires_at`; a newer link or /logout sets
    `revoked_at` on the links not used yet."""

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_login_tokens_hash", "token_hash", unique=True),)


class WebSession(Base):
    """A browser signed in by a one-time link: the cookie holds a random value, the base only its SHA-256. Ends at
    `expires_at`, on «Выйти» or on /logout in the bot (`revoked_at`)."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    login_token_id: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_web_sessions_hash", "session_hash", unique=True),)
