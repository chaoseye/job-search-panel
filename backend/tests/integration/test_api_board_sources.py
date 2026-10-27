"""The board «Воронка» for tenders and studios (the third step of variant «Б + Б3»): which tender or studio lands in
which column and band, the counts, the order of a column, the funnel, the validation of the source's columns, and
cards moving on through the usual /api/tenders and /api/studios endpoints."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    CandidateProfile,
    Studio,
    StudioLetter,
    StudioLetterRequest,
    WsTender,
    WsTenderReply,
    WsTenderReplyRequest,
)
from app.main import app
from app.runtime import Runtime

WS = "https://workspace.ru"
FUTURE = "2099-12-31"
PAST = "2020-01-01"


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(
        settings.model_copy(update={"batch_auto_create": False, "ws_tenders_sync_min": 0, "timezone": "UTC"})
    )
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _keys(items: list[dict[str, Any]], kind: str) -> list[str]:
    return [i[kind]["title" if kind == "tender" else "name"] for i in items]


async def _seed_tenders(runtime: Runtime) -> dict[str, int]:
    """A tender per place of the tender board; key → id (the key is its title too)."""
    now = utcnow()
    async with runtime.session_factory() as session:
        session.add(
            CandidateProfile(version=1, is_active=True, preferences_json="{}", profile_md="# профиль", hash="h")
        )

        def tender(key: str, n: int, **extra: Any) -> WsTender:
            values: dict[str, Any] = {"state": "open", "deadline_on": FUTURE, "first_seen_at": now - timedelta(days=2)}
            values.update(extra)
            return WsTender(ws_id=n, url=f"{WS}/tenders/{key}-{n}/", title=key, **values)

        rows = {
            "лучший": tender("лучший", 1, relevant=True, fit_score=80, published_on="2026-10-20", first_seen_at=now),
            "интересный": tender("интересный", 2, relevant=True, fit_score=60, status="interesting"),
            "другой": tender("другой", 3, relevant=False, fit_score=10),
            # the deadline has passed since the last rescore: the stored flag still says suitable
            "просроченный": tender("просроченный", 4, relevant=True, fit_score=70, deadline_on=PAST),
            "закрытый": tender("закрытый", 5, relevant=True, fit_score=90, state="closed"),
            "пишется": tender("пишется", 6, relevant=True, status="drafting", deadline_on="2099-01-01"),
            "готов": tender("готов", 7, relevant=True, status="drafted", deadline_on="2099-06-01"),
            "готов-поздно": tender("готов-поздно", 8, relevant=False, status="drafted", deadline_on=PAST),
            "откликнулся": tender("откликнулся", 9, status="responded", responded_at=now - timedelta(hours=3)),
            "ответили": tender(
                "ответили",
                12,
                status="replied",
                responded_at=now - timedelta(days=2),
                answered_at=now - timedelta(hours=1),
            ),
            "отказ": tender(
                "отказ",
                13,
                status="declined",
                responded_at=now - timedelta(days=3),
                answered_at=now - timedelta(hours=5),
            ),
            "пропущен": tender("пропущен", 10, relevant=True, status="skipped"),
            "старый": tender("старый", 11, relevant=True, status="skipped", first_seen_at=now - timedelta(days=45)),
        }
        session.add_all(rows.values())
        await session.flush()
        session.add(WsTenderReplyRequest(tender_id=rows["пишется"].id))
        session.add(WsTenderReply(tender_id=rows["готов"].id, version=1, origin="claude", text="Здравствуйте! " * 5))
        session.add(WsTenderReply(tender_id=rows["готов-поздно"].id, version=1, origin="claude", text="Привет " * 5))
        await session.commit()
        return {k: r.id for k, r in rows.items()}


async def test_tender_board_columns_bands_and_funnel(api) -> None:
    client, runtime = api
    ids = await _seed_tenders(runtime)
    data = (await client.get("/api/board", params={"source": "tenders"})).json()
    assert data["source"] == "tenders"
    # «просроченный» and «закрытый» left «Найдено», «пропущен» is nowhere
    assert data["bands"] == {"relevant": 2, "other": 1}
    assert data["fresh_today"] == 1
    columns = {c["key"]: c for c in data["columns"]}
    assert [c["key"] for c in data["columns"]] == ["new", "ready", "responded", "answer"]
    assert {k: c["count"] for k, c in columns.items()} == {"new": 2, "ready": 3, "responded": 1, "answer": 2}
    assert _keys(columns["new"]["items"], "tender") == ["лучший", "интересный"]
    # what can still be posted first, a written answer before one being written, a closed one last
    assert _keys(columns["ready"]["items"], "tender") == ["готов", "пишется", "готов-поздно"]
    ready = columns["ready"]["items"][0]
    assert ready["tender"]["status"] == "drafted" and ready["stage_at"]
    assert columns["ready"]["items"][2]["tender"]["open"] is False
    assert columns["responded"]["items"][0]["stage_at"] == columns["responded"]["items"][0]["tender"]["responded_at"]
    # the latest answer first, it came to the column when it was recorded
    assert _keys(columns["answer"]["items"], "tender") == ["ответили", "отказ"]
    assert columns["answer"]["items"][0]["stage_at"] == columns["answer"]["items"][0]["tender"]["answered_at"]

    funnel = {s["key"]: s["value"] for s in data["funnel"]}
    # «старый» was found 45 days ago; suitable: the relevant ones (a skipped one too) and the ones in work; the answers
    # written, the responses marked (the answered ones too) and the organizers' answers recorded in the period
    assert funnel == {"found": 12, "suitable": 11, "ready": 2, "responded": 3, "answered": 2}

    other = (await client.get("/api/board/column/new", params={"source": "tenders", "band": "other"})).json()
    assert (other["band"], other["total"], _keys(other["items"], "tender")) == ("other", 1, ["другой"])
    page = (await client.get("/api/board/column/ready", params={"source": "tenders", "offset": 1, "limit": 1})).json()
    assert (page["total"], page["offset"], _keys(page["items"], "tender")) == (3, 1, ["пишется"])
    assert ids["лучший"] == columns["new"]["items"][0]["tender"]["id"]


async def test_source_columns_and_bands_are_checked(api) -> None:
    client, _runtime = api
    assert (await client.get("/api/board", params={"source": "channels"})).status_code == 422
    assert (await client.get("/api/board/column/confirm", params={"source": "tenders"})).status_code == 422
    assert (await client.get("/api/board/column/responded")).status_code == 422  # a tender column, not a vacancy one
    assert (await client.get("/api/board/column/new", params={"source": "tenders", "band": "hi"})).status_code == 400
    assert (
        await client.get("/api/board/column/ready", params={"source": "tenders", "band": "relevant"})
    ).status_code == 400
    assert (
        await client.get("/api/board/column/new", params={"source": "studios", "band": "relevant"})
    ).status_code == 400
    assert (await client.get("/api/board/column/viewed", params={"source": "studios"})).status_code == 422
    vacancies = (await client.get("/api/board")).json()
    assert vacancies["source"] == "vacancies" and [c["key"] for c in vacancies["columns"]][0] == "new"


async def test_a_tender_walks_the_board_through_the_usual_endpoints(api) -> None:
    client, runtime = api
    ids = await _seed_tenders(runtime)

    async def column(key: str, band: str | None = None) -> list[str]:
        params = {"source": "tenders", **({"band": band} if band else {})}
        return _keys((await client.get(f"/api/board/column/{key}", params=params)).json()["items"], "tender")

    r = await client.post("/api/tenders/replies", json={"ids": [ids["лучший"]]})
    assert r.status_code == 200 and r.json()["queued"] == 1, r.text
    assert "лучший" not in await column("new") and "лучший" in await column("ready")
    assert (await client.post(f"/api/tenders/{ids['готов']}/responded")).json()["status"] == "responded"
    assert await column("responded") == ["готов", "откликнулся"]
    assert (await client.post(f"/api/tenders/{ids['готов']}/replied")).json()["status"] == "replied"
    assert await column("responded") == ["откликнулся"] and await column("answer") == ["готов", "ответили", "отказ"]
    assert (await client.post(f"/api/tenders/{ids['откликнулся']}/declined")).json()["status"] == "declined"
    assert await column("responded") == [] and (await column("answer"))[0] == "откликнулся"
    assert (await client.post(f"/api/tenders/{ids['интересный']}/skip")).status_code == 200
    assert await column("new") == []
    assert (await client.post(f"/api/tenders/{ids['другой']}/hide")).status_code == 200
    assert await column("new", "other") == []


async def _seed_studios(runtime: Runtime) -> dict[str, int]:
    """A studio per place of the studio board; key → id (the key is its name too)."""
    now = utcnow()
    async with runtime.session_factory() as session:
        session.add(
            CandidateProfile(version=1, is_active=True, preferences_json="{}", profile_md="# профиль", hash="h")
        )

        def studio(key: str, status: str, **extra: Any) -> Studio:
            values: dict[str, Any] = {"contacts_checked_at": now}
            values.update(extra)
            return Studio(name=key, source="list", status=status, **values)

        mail = '["hello@studio.example"]'
        rows = {
            "почта": studio("почта", "ready", emails_json=mail, fit_score=80),
            "телеграм": studio("телеграм", "ready", telegram="https://t.me/example_studio", fit_score=50),
            "непроверенная": studio("непроверенная", "new", contacts_checked_at=None),
            "без-контактов": studio("без-контактов", "no_contacts"),
            "пишется": studio("пишется", "drafting", emails_json=mail, fit_score=90),
            "готово": studio("готово", "drafted", emails_json=mail, fit_score=40),
            "отправлено": studio("отправлено", "sent", emails_json=mail, sent_at=now - timedelta(days=1)),
            "ответили": studio(
                "ответили", "replied", emails_json=mail, sent_at=now - timedelta(days=3), replied_at=now
            ),
            "отказ": studio("отказ", "declined", telegram="@example_studio", sent_at=now - timedelta(days=2)),
            "скрытая": studio("скрытая", "hidden", emails_json=mail),
        }
        session.add_all(rows.values())
        await session.flush()
        session.add(StudioLetterRequest(studio_id=rows["пишется"].id))
        session.add(
            StudioLetter(
                studio_id=rows["готово"].id, version=1, style="business", subject="Python-разработчик", origin="claude",
                text="Здравствуйте! " * 10,
            )
        )  # fmt: skip
        await session.commit()
        return {k: r.id for k, r in rows.items()}


async def test_studio_board_columns_bands_and_funnel(api) -> None:
    client, runtime = api
    await _seed_studios(runtime)
    data = (await client.get("/api/board", params={"source": "studios"})).json()
    assert data["source"] == "studios"
    assert data["bands"] == {"contacts": 2, "unchecked": 1, "nocontacts": 1}
    assert (data["sent_today"], data["daily_limit"]) == (0, 20)
    columns = {c["key"]: c for c in data["columns"]}
    assert [c["key"] for c in data["columns"]] == ["new", "ready", "sent", "answer"]
    # «Найдено» counts the studios a step is possible for right away: with contacts and not checked yet
    assert {k: c["count"] for k, c in columns.items()} == {"new": 3, "ready": 2, "sent": 1, "answer": 2}
    assert _keys(columns["new"]["items"], "studio") == ["почта", "телеграм"]
    assert _keys(columns["ready"]["items"], "studio") == ["готово", "пишется"]  # the written letter first
    assert _keys(columns["answer"]["items"], "studio") == ["ответили", "отказ"]
    sent = columns["sent"]["items"][0]
    assert sent["stage_at"] == sent["studio"]["sent_at"]

    funnel = {s["key"]: s["value"] for s in data["funnel"]}
    assert funnel == {"work": 9, "contacts": 7, "letter": 4, "sent": 3, "answered": 2}

    for band, names in (("unchecked", ["непроверенная"]), ("nocontacts", ["без-контактов"])):
        page = (await client.get("/api/board/column/new", params={"source": "studios", "band": band})).json()
        assert (page["band"], _keys(page["items"], "studio")) == (band, names)


async def test_a_studio_walks_the_board_through_the_usual_endpoints(api) -> None:
    client, runtime = api
    ids = await _seed_studios(runtime)

    async def column(key: str) -> list[str]:
        return _keys(
            (await client.get(f"/api/board/column/{key}", params={"source": "studios"})).json()["items"], "studio"
        )

    r = await client.post("/api/studios/letters", json={"ids": [ids["почта"]]})
    assert r.status_code == 200 and r.json()["queued"] == 1, r.text
    assert "почта" not in await column("new") and "почта" in await column("ready")
    r = await client.post(f"/api/studios/{ids['готово']}/sent", json={"to": "hello@studio.example", "via": "email"})
    assert r.status_code == 200 and r.json()["sent_today"] == 1, r.text
    assert await column("sent") == ["готово", "отправлено"]
    assert (await client.get("/api/board", params={"source": "studios"})).json()["sent_today"] == 1
    assert (await client.post(f"/api/studios/{ids['отправлено']}/replied")).json()["status"] == "replied"
    assert (await column("answer"))[0] == "отправлено"
    assert (await client.post(f"/api/studios/{ids['телеграм']}/skip")).status_code == 200
    assert await column("new") == []
