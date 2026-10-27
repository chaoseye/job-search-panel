"""hh.ru notification e-mails → the manual journal (IMAP, read-only).

hh closed the negotiations API for the application, but it still e-mails the applicant about every step:
«работодатель просмотрел отклик», «приглашение», «отказ», «новое сообщение». The panel reads the mailbox
(`IMAP_HOST/USER/PASSWORD`, an app password, only in `.env`), keeps the messages untouched (BODY.PEEK, no flags),
classifies each hh message by its subject/body, matches it to a sent application — by the vacancy link in the
message, else by the vacancy title — and writes the same journal entry the user would write by hand
(`manual_negotiation.record_update`). Progress is a UID cursor plus the Message-IDs already applied, so nothing
is recorded twice. Unmatched hh messages are listed in Settings (subject only) for a manual entry.

The same run looks for answers to the letters the user sent to studios (services/studios.py): one IMAP search per
studio, messages from the studio's domain (or from the exact address when it is a public mail service) since the
letter went out; the studio becomes «ответили» and the event `studio_reply` reaches the feed and Telegram.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Application, Vacancy
from app.hh.textclean import html_to_text
from app.hh.vacancy import HH_VACANCY_LINK
from app.services.audit import audit
from app.services.manual_negotiation import ManualError, record_update
from app.services.notify import emit_event
from app.services.state import get_setting, set_setting
from app.services.textmatch import norm

log = structlog.get_logger("mail_sync")

UID_SETTING = "mail_sync_uid"
SEEN_SETTING = "mail_sync_seen"  # Message-IDs already applied (last SEEN_KEEP)
STATUS_SETTING = "mail_sync_status"
UNMATCHED_SETTING = "mail_sync_unmatched"  # [{subject, at}] — last UNMATCHED_KEEP for the Settings page
SEEN_KEEP = 1000
UNMATCHED_KEEP = 20
FETCH_LIMIT = 200
SNIPPET_CHARS = 700
# subject/body keywords → journal kind; the order matters (an invitation mail may also say «отклик просмотрен»)
KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invitation", ("приглаш", "приглас", "invitation", "invited")),
    ("discard", ("отказ", "отклонил", "отклонён", "отклонен", "не готов", "rejected", "declined")),
    ("viewed", ("просмотр", "посмотрел", "viewed")),
    ("message", ("сообщени", "написал", "ответил", "message", "чат")),
)
KIND_LABELS = {"invitation": "приглашение", "discard": "отказ", "viewed": "просмотр", "message": "сообщение"}


@dataclass
class MailItem:
    uid: int
    message_id: str
    subject: str
    sender: str
    date: datetime | None
    text: str
    links: list[str] = field(default_factory=list)


def configured(settings: Settings) -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_password)


def masked_user(settings: Settings) -> str | None:
    user = settings.imap_user or ""
    if not user:
        return None
    name, _, domain = user.partition("@")
    return f"{name[:2]}***@{domain}" if domain else f"{name[:2]}***"


# ---- parsing ----------------------------------------------------------------------------


def classify(subject: str, text: str) -> str | None:
    """Journal kind by the subject first, then by the beginning of the body; None = not about an application."""
    for source in (norm(subject), norm(text[:600])):
        if not source:
            continue
        for kind, keys in KINDS:
            if any(k in source for k in keys):
                return kind
    return None


def vacancy_ids(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in HH_VACANCY_LINK.finditer(t or ""):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def parse_message(raw: bytes, uid: int) -> MailItem:
    msg = email.message_from_bytes(raw, policy=default_policy)
    subject = str(msg.get("Subject") or "").strip()
    sender = str(msg.get("From") or "").strip()
    message_id = str(msg.get("Message-ID") or "").strip() or f"uid:{uid}"
    date: datetime | None = None
    try:
        raw_date = msg.get("Date")
        date = email.utils.parsedate_to_datetime(str(raw_date)) if raw_date else None
    except (TypeError, ValueError):
        date = None
    text = ""
    links: list[str] = []
    if isinstance(msg, EmailMessage):
        html_part = msg.get_body(preferencelist=("html",))
        plain_part = msg.get_body(preferencelist=("plain",))
        html = html_part.get_content() if html_part is not None else ""
        plain = plain_part.get_content() if plain_part is not None else ""
        links = re.findall(r"https?://[^\s\"'<>]+", html) + re.findall(r"https?://[^\s\"'<>]+", plain)
        text = plain.strip() or html_to_text(html)
    return MailItem(
        uid=uid, message_id=message_id, subject=subject, sender=sender, date=as_utc(date), text=text, links=links
    )


def snippet(text: str) -> str:
    """Body without link lines and hh boilerplate, for a `message` journal entry."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or "http" in s or s.startswith(("•", "©")):
            continue
        if any(k in s.lower() for k in ("отписаться", "unsubscribe", "настройки уведомлений", "hh.ru,", "headhunter")):
            continue
        lines.append(s)
    out = " ".join(lines)
    return out[: SNIPPET_CHARS - 1].rstrip() + "…" if len(out) > SNIPPET_CHARS else out


# ---- IMAP ---------------------------------------------------------------------------------


def _fetch_sync(settings: Settings, last_uid: int | None, since: datetime) -> tuple[list[MailItem], int | None]:
    """Blocking IMAP session: new hh messages after `last_uid` (or since a date on the first run)."""
    items: list[MailItem] = []
    max_uid = last_uid
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as box:
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder or "INBOX", readonly=True)
        sender = (settings.imap_from or "hh.ru").replace('"', "")
        if last_uid:
            criteria = f'(UID {last_uid + 1}:* FROM "{sender}")'
        else:
            criteria = f'(SINCE {since.strftime("%d-%b-%Y")} FROM "{sender}")'
        typ, data = box.uid("search", criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        uids = [int(u) for u in (data[0] or b"").split() if u.isdigit()]
        uids = [u for u in uids if not last_uid or u > last_uid][:FETCH_LIMIT]
        for uid in uids:
            typ, parts = box.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not parts:
                continue
            raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
            if not isinstance(raw, bytes):
                continue
            items.append(parse_message(raw, uid))
            max_uid = uid if max_uid is None else max(max_uid, uid)
    return items, max_uid


def _studio_replies_sync(
    settings: Settings, targets: list[tuple[int, str, datetime]]
) -> list[tuple[int, str, datetime | None]]:
    """Blocking IMAP session: for each (studio id, FROM needle, sent at) the newest message from the studio since."""
    found: list[tuple[int, str, datetime | None]] = []
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as box:
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder or "INBOX", readonly=True)
        for studio_id, needle, since in targets:
            criteria = f'(SINCE {since.strftime("%d-%b-%Y")} FROM "{needle.replace(chr(34), "")}")'
            typ, data = box.uid("search", criteria)
            if typ != "OK":
                continue
            uids = [int(u) for u in (data[0] or b"").split() if u.isdigit()]
            if not uids:
                continue
            typ, parts = box.uid("fetch", str(max(uids)), "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            raw = (
                next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None) if typ == "OK" else None
            )
            subject, date = "", None
            if isinstance(raw, bytes):
                msg = email.message_from_bytes(raw, policy=default_policy)
                subject = str(msg.get("Subject") or "").strip()
                try:
                    date = email.utils.parsedate_to_datetime(str(msg.get("Date"))) if msg.get("Date") else None
                except (TypeError, ValueError):
                    date = None
            when = as_utc(date)
            # IMAP SINCE is by day: a message of the same day but from before the letter is not an answer
            if when is not None and when < since:
                continue
            found.append((studio_id, subject, when))
    return found


async def fetch_new(settings: Settings, last_uid: int | None, since: datetime) -> tuple[list[MailItem], int | None]:
    return await asyncio.to_thread(_fetch_sync, settings, last_uid, since)


# ---- applying ------------------------------------------------------------------------------


async def _sent_applications(session: AsyncSession) -> list[tuple[Application, Vacancy]]:
    rows = (
        await session.execute(
            select(Application, Vacancy)
            .join(Vacancy, Vacancy.id == Application.vacancy_id)
            .where(Application.status == "sent", Application.dry_run.is_(False))
            .order_by(Application.sent_at.desc())
        )
    ).all()
    return [(a, v) for a, v in rows]


def match_application(item: MailItem, apps: list[tuple[Application, Vacancy]]) -> Application | None:
    """By the vacancy link in the mail, else by a unique vacancy title mentioned in the subject/body."""
    ids = vacancy_ids(*item.links, item.text, item.subject)
    for hh_id in ids:
        for app_row, vacancy in apps:
            if vacancy.hh_id == hh_id:
                return app_row
    haystack = norm(item.subject + "\n" + item.text[:3000])
    hits = [
        (app_row, vacancy)
        for app_row, vacancy in apps
        if vacancy.name and len(norm(vacancy.name)) >= 6 and norm(vacancy.name) in haystack
    ]
    if not hits:
        return None
    names = {v.hh_id for _, v in hits}
    if len(names) == 1:
        return hits[0][0]
    # several applications share the mentioned title: the employer name decides
    for app_row, vacancy in hits:
        if vacancy.employer_name and norm(vacancy.employer_name) in haystack:
            return app_row
    return None


async def apply_items(session: AsyncSession, settings: Settings, items: list[MailItem]) -> dict[str, Any]:
    seen_raw = await get_setting(session, SEEN_SETTING) or []
    seen: list[str] = [str(x) for x in seen_raw] if isinstance(seen_raw, list) else []
    unmatched_raw = await get_setting(session, UNMATCHED_SETTING) or []
    unmatched: list[dict[str, Any]] = list(unmatched_raw) if isinstance(unmatched_raw, list) else []
    apps = await _sent_applications(session)
    vacancies = {a.id: v for a, v in apps}
    summary: dict[str, Any] = {"fetched": len(items), "matched": 0, "unmatched": 0, "ignored": 0, "duplicates": 0}
    invited: list[Application] = []
    messaged: list[tuple[Application, Any]] = []
    for item in sorted(items, key=lambda i: i.uid):
        if item.message_id in seen:
            summary["duplicates"] += 1
            continue
        kind = classify(item.subject, item.text)
        if kind is None:
            summary["ignored"] += 1
            seen.append(item.message_id)
            continue
        app_row = match_application(item, apps)
        if app_row is None:
            summary["unmatched"] += 1
            seen.append(item.message_id)
            unmatched.append(
                {
                    "subject": item.subject[:200],
                    "kind": kind,
                    "at": (item.date or utcnow()).isoformat(timespec="minutes"),
                }
            )
            continue
        text = snippet(item.text) if kind == "message" else None
        try:
            negotiation = await record_update(
                session,
                app_row,
                state=kind if kind in ("invitation", "discard") else None,
                viewed=True if kind == "viewed" else None,
                text=text,
                author="employer",
                occurred_at=item.date,
            )
        except ManualError as exc:
            summary["unmatched"] += 1
            unmatched.append({"subject": item.subject[:200], "kind": kind, "error": str(exc)})
            seen.append(item.message_id)
            continue
        summary["matched"] += 1
        seen.append(item.message_id)
        if kind == "invitation":
            invited.append(app_row)
        if kind == "message":
            messaged.append((app_row, negotiation))
        if kind in ("invitation", "discard", "message"):
            # the same Telegram events the hh sync would send (the chat link is the vacancy page: no hh chat id)
            vacancy = vacancies.get(app_row.id)
            await emit_event(
                session,
                settings,
                kind,
                {
                    "name": vacancy.name if vacancy is not None else "вакансия",
                    "employer": vacancy.employer_name if vacancy is not None else None,
                    "url": vacancy.alternate_url if vacancy is not None else None,
                    "vacancy_id": app_row.vacancy_id,
                    "text": text,
                    "author": "работодатель",
                    "source": "mail",
                },
                now=item.date,
            )
        await audit(
            session,
            "negotiations",
            f"Из письма hh.ru: {KIND_LABELS[kind]} — «{item.subject[:120]}»",
            application_id=app_row.id,
        )
    await set_setting(session, SEEN_SETTING, seen[-SEEN_KEEP:])
    await set_setting(session, UNMATCHED_SETTING, unmatched[-UNMATCHED_KEEP:])
    if invited and settings.interview_prep_auto:
        from app.services.interview_prep import request_interview_prep

        for app_row in invited:
            try:
                await request_interview_prep(session, settings, app_row, notes=None, auto=True)
            except Exception as exc:  # noqa: BLE001 - the journal entry is the important part
                log.warning("interview_prep_auto_failed", application_id=app_row.id, error=str(exc))
    if messaged and settings.reply_draft_auto:
        from app.services.sync import request_reply_draft

        for app_row, negotiation in messaged:
            try:
                await request_reply_draft(session, settings, negotiation, None)
                summary["reply_drafts"] = int(summary.get("reply_drafts", 0)) + 1
            except Exception as exc:  # noqa: BLE001
                log.warning("reply_draft_auto_failed", application_id=app_row.id, error=str(exc))
    return summary


async def sync_mail(runtime: Any) -> dict[str, Any]:
    """Job `mail_sync`: fetch new hh messages and apply them; status is kept in settings for the UI."""
    settings: Settings = runtime.settings
    if not configured(settings):
        return {"skipped": "IMAP не настроен"}
    started = utcnow()
    async with runtime.session_factory() as session:
        last_uid = await get_setting(session, UID_SETTING)
        last_uid = int(last_uid) if isinstance(last_uid, int | str) and str(last_uid).isdigit() else None
    since = started - timedelta(days=max(1, settings.imap_since_days))
    try:
        items, max_uid = await fetch_new(settings, last_uid, since)
    except Exception as exc:  # noqa: BLE001 - network/auth errors are reported, never raised with the password
        message = _safe_error(settings, exc)
        async with runtime.session_factory() as session:
            await set_setting(session, STATUS_SETTING, {"at": started.isoformat(timespec="seconds"), "error": message})
            await session.commit()
        log.warning("mail_sync_failed", error=message)
        return {"error": message}
    async with runtime.session_factory() as session:
        summary = await apply_items(session, settings, items)
        if max_uid is not None:
            await set_setting(session, UID_SETTING, max_uid)
        summary["last_uid"] = max_uid
        await set_setting(
            session, STATUS_SETTING, {"at": started.isoformat(timespec="seconds"), "error": None, "summary": summary}
        )
        await session.commit()
    summary["studio_replies"] = await _studio_replies(runtime)
    if summary["matched"] or summary["unmatched"] or summary["studio_replies"]:
        log.info("mail_sync", **summary)
    return summary


async def _studio_replies(runtime: Any) -> int:
    from app.services import studios

    settings: Settings = runtime.settings
    async with runtime.session_factory() as session:
        targets = await studios.reply_targets(session)
    if not targets:
        return 0
    try:
        replies = await asyncio.to_thread(_studio_replies_sync, settings, targets)
    except Exception as exc:  # noqa: BLE001 - the hh part of the sync is done; this one is retried next time
        log.warning("studio_replies_failed", error=_safe_error(settings, exc))
        return 0
    async with runtime.session_factory() as session:
        recorded = await studios.record_replies(session, settings, replies)
        await session.commit()
    return recorded


def _safe_error(settings: Settings, exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in (settings.imap_password, settings.imap_user):
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


async def mail_status(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    status = await get_setting(session, STATUS_SETTING) or {}
    unmatched = await get_setting(session, UNMATCHED_SETTING) or []
    return {
        "configured": configured(settings),
        "host": settings.imap_host or None,
        "user": masked_user(settings),
        "folder": settings.imap_folder or "INBOX",
        "interval_min": settings.imap_sync_min,
        "since_days": settings.imap_since_days,
        "sender_filter": settings.imap_from,
        "last": status if isinstance(status, dict) else {},
        "unmatched": list(reversed(unmatched)) if isinstance(unmatched, list) else [],
        "interview_prep_auto": settings.interview_prep_auto,
    }
