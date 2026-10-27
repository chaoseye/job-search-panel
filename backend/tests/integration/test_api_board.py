"""The board «Воронка» (variant «Б + Б3»): which vacancy lands in which column and band, the counts, the order of a
column, the 30-day funnel, and a card walking the whole way through the usual endpoints (open on hh.ru → confirm →
viewed → invitation)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Application, CoverLetter, Negotiation, Vacancy, VacancyEvaluation
from app.main import app
from app.runtime import Runtime

GOOD_LETTER = "Здравствуйте! Я делаю автоматизацию на Python и внедряю языковые модели в процессы компании. " * 3
ASKING_LETTER = "Здравствуйте! С Bitrix24 работаю [[УТОЧНИТЬ: сколько лет]], настраивал обмен с 1С. " * 3


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False, "timezone": "UTC"}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _seed(runtime: Runtime) -> dict[str, int]:
    """One vacancy per place of the board, with the letter, the evaluation, the application and the negotiation that
    put it there; key → vacancy id."""
    now = utcnow()
    ids: dict[str, int] = {}
    async with runtime.session_factory() as session:

        def vacancy(key: str, status: str, *, score: int | None = None, prescore: int | None = 60, **extra) -> Vacancy:
            row = Vacancy(
                hh_id=str(700000 + len(ids)),
                name=f"Вакансия {key}",
                employer_id=f"e-{key}",
                employer_name=f"Компания {key}",
                pipeline_status=status,
                score=score,
                prescore=prescore,
                published_at=extra.pop("published_at", now - timedelta(days=2)),
                first_seen_at=extra.pop("first_seen_at", now - timedelta(days=2)),
                last_seen_at=now,
                **extra,
            )
            session.add(row)
            return row

        async def add(key: str, row: Vacancy) -> Vacancy:
            await session.flush()
            ids[key] = row.id
            return row

        def evaluation(row: Vacancy, score: int, *, days_ago: int = 1) -> None:
            verdict = "apply" if score >= 70 else "maybe" if score >= 50 else "skip"
            session.add(
                VacancyEvaluation(
                    vacancy_id=row.id,
                    batch_id="20261020_1200_0000",
                    score=score,
                    verdict=verdict,
                    is_current=True,
                    created_at=now - timedelta(days=days_ago),
                )
            )

        def letter(row: Vacancy, text: str = GOOD_LETTER) -> None:
            session.add(CoverLetter(vacancy_id=row.id, version=1, text=text, origin="claude", is_current=True))

        def sent(row: Vacancy, *, days_ago: int, neg: dict | None = None) -> Application:
            app_row = Application(
                vacancy_id=row.id,
                mode="assist",
                status="sent",
                letter_text=GOOD_LETTER,
                sent_at=now - timedelta(days=days_ago),
                queued_at=now - timedelta(days=days_ago),
                confirmed_by_user=True,
            )
            session.add(app_row)
            return app_row

        # «Новые»: Claude's bands, the fresh one, a letter with a question, the put off and the hidden ones
        s1 = await add("suitable_today", vacancy("suitable_today", "suitable", score=85, first_seen_at=now))
        evaluation(s1, 85)
        letter(s1)
        s2 = await add("suitable_asks", vacancy("suitable_asks", "suitable", score=72))
        evaluation(s2, 72)
        letter(s2, ASKING_LETTER)
        m1 = await add("maybe", vacancy("maybe", "maybe", score=60))
        evaluation(m1, 60)
        letter(m1)
        low = await add("rejected", vacancy("rejected", "not_suitable", score=30))
        evaluation(low, 30)
        await add("below_prescore", vacancy("below_prescore", "not_suitable", score=None, prescore=20))
        await add("prescored", vacancy("prescored", "prescored"))
        await add("in_packet", vacancy("in_packet", "awaiting_claude", batch_id="20261024_0100_abcd"))
        await add("manual_step", vacancy("manual_step", "needs_manual"))
        snoozed = await add("snoozed", vacancy("snoozed", "suitable", score=90, snoozed_until=now + timedelta(days=2)))
        evaluation(snoozed, 90)
        await add("hidden", vacancy("hidden", "suitable", score=95, hidden=True))
        await add("watched", vacancy("watched", "watched", prescore=None, first_seen_at=now - timedelta(days=1)))
        await add("old", vacancy("old", "skipped", first_seen_at=now - timedelta(days=45)))

        # «Ждут подтверждения»: an assist page left open, an API queue; a page left open on a closed vacancy is not one
        c1 = await add("assist_open", vacancy("assist_open", "needs_manual", score=80))
        letter(c1)
        session.add(
            Application(vacancy_id=c1.id, mode="assist", status="needs_manual", queued_at=now - timedelta(days=3))
        )
        c2 = await add("api_queue", vacancy("api_queue", "queued", score=75))
        session.add(Application(vacancy_id=c2.id, mode="api", status="queued", queued_at=now - timedelta(hours=2)))
        closed = await add("closed_open", vacancy("closed_open", "filtered_out", score=70))
        session.add(Application(vacancy_id=closed.id, mode="assist", status="needs_manual", queued_at=now))

        # responses: no reaction, viewed, invitation, rejection, a message, viewed and then a message
        plain = await add("sent_plain", vacancy("sent_plain", "sent", score=77))
        sent(plain, days_ago=1)
        stages = {
            "sent_viewed": dict(viewed_by_opponent=True, first_viewed_at=now - timedelta(days=3)),
            "sent_invited": dict(
                state_id="invitation",
                invited_at=now - timedelta(days=2),
                first_reaction_at=now - timedelta(days=2),
                viewed_by_opponent=True,
            ),
            "sent_rejected": dict(
                state_id="discard", discarded_at=now - timedelta(days=1), first_reaction_at=now - timedelta(days=1)
            ),
            "sent_message": dict(
                last_employer_message_at=now - timedelta(hours=5), first_reaction_at=now - timedelta(hours=5)
            ),
            "sent_viewed_then_message": dict(
                viewed_by_opponent=True,
                first_viewed_at=now - timedelta(days=6),
                last_employer_message_at=now - timedelta(days=4),
                first_reaction_at=now - timedelta(days=4),
            ),
        }
        for n, (key, fields) in enumerate(stages.items(), start=2):
            row = await add(key, vacancy(key, "sent", score=70 + n))
            app_row = sent(row, days_ago=n + 5)
            await session.flush()
            hh_id = f"manual-{app_row.id}"
            app_row.hh_negotiation_id = hh_id
            session.add(
                Negotiation(
                    hh_id=hh_id,
                    vacancy_id=row.id,
                    application_id=app_row.id,
                    state_id=fields.pop("state_id", "response"),
                    **fields,
                )
            )
        await session.commit()
    return ids


def _names(items: list[dict]) -> list[str]:
    return [it["vacancy"]["name"].removeprefix("Вакансия ") for it in items]


async def test_board_columns_counts_and_funnel(api) -> None:
    client, runtime = api
    await _seed(runtime)
    r = await client.get("/api/board")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["thresholds"] == {"suitable": 70, "maybe": 50}
    columns = {c["key"]: c for c in data["columns"]}
    assert [c["key"] for c in data["columns"]] == ["new", "confirm", "sent", "viewed", "answer"]
    assert [c["title"] for c in data["columns"]] == ["Новые", "Ждут подтверждения", "Отправлено", "Смотрели", "Ответ"]

    # «Новые»: evaluated and worth a decision; the bands around them in numbers
    assert data["new"] == {
        "hi": 2,
        "mid": 1,
        "low": 1,
        "wait": 2,
        "manual": 1,
        "fresh_today": 1,
        "clarify": 1,
        "snoozed": 1,
    }
    assert columns["new"]["count"] == 3
    assert _names(columns["new"]["items"]) == ["suitable_today", "suitable_asks"]  # the two best by the score
    first = columns["new"]["items"][0]
    assert first["application"] is None and first["vacancy"]["letter_status"] == "ready"
    assert columns["new"]["items"][1]["vacancy"]["letter_status"] == "needs_clarification"

    assert columns["confirm"]["count"] == 2
    assert _names(columns["confirm"]["items"]) == ["api_queue", "assist_open"]  # the newest first
    assert [it["application"]["status"] for it in columns["confirm"]["items"]] == ["queued", "needs_manual"]
    assert columns["sent"]["count"] == 1 and _names(columns["sent"]["items"]) == ["sent_plain"]
    assert columns["viewed"]["count"] == 1 and _names(columns["viewed"]["items"]) == ["sent_viewed"]
    assert columns["answer"]["count"] == 4
    # the freshest reaction first, and what it was
    answers = columns["answer"]["items"]
    assert _names(answers) == ["sent_message", "sent_rejected", "sent_invited", "sent_viewed_then_message"]
    assert [it["outcome"] for it in answers] == ["message", "discard", "invitation", "message"]
    assert answers[0]["application"]["negotiation"]["hh_id"].startswith("manual-")

    # 30 days: the watch-only search and the vacancy found 45 days ago stay out; evaluations from 50; sent and after
    funnel = {f["key"]: f["value"] for f in data["funnel"]}
    assert funnel == {"found": 19, "good": 4, "sent": 6, "viewed": 3, "invited": 1}
    assert [f["label"] for f in data["funnel"]][1] == "оценка от 50"


async def test_column_lists_bands_filters_and_validation(api) -> None:
    client, runtime = api
    await _seed(runtime)

    async def column(key: str, **params) -> dict:
        r = await client.get(f"/api/board/column/{key}", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    hi = await column("new")
    assert hi["band"] == "hi" and hi["total"] == 2 and _names(hi["items"]) == ["suitable_today", "suitable_asks"]
    assert _names((await column("new", band="clarify"))["items"]) == ["suitable_asks"]  # hi and mid, a question asked
    assert _names((await column("new", band="mid"))["items"]) == ["maybe"]
    assert _names((await column("new", band="low"))["items"]) == ["rejected"]  # rejected by Claude, not by prescore
    assert sorted(_names((await column("new", band="wait"))["items"])) == ["in_packet", "prescored"]
    assert _names((await column("new", band="manual"))["items"]) == ["manual_step"]
    # paging keeps the order
    page = await column("answer", offset=1, limit=2)
    assert page["total"] == 4 and _names(page["items"]) == ["sent_rejected", "sent_invited"]
    viewed = await column("viewed")
    assert viewed["band"] is None and viewed["items"][0]["stage_at"]  # when the employer saw it

    assert (await client.get("/api/board/column/new", params={"band": "bogus"})).status_code == 400
    assert (await client.get("/api/board/column/sent", params={"band": "hi"})).status_code == 400
    assert (await client.get("/api/board/column/elsewhere")).status_code == 422
    assert (await client.get("/api/board/column/new", params={"limit": 0})).status_code == 422


async def test_a_card_walks_the_board_through_the_usual_endpoints(api) -> None:
    client, runtime = api
    ids = await _seed(runtime)
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    assert (await client.post("/api/applications/consent")).status_code == 200

    async def where(name: str) -> str | None:
        """The column the vacancy is in (for «Новые» — in its decision bands), None when it left the board."""
        for key, params in [
            ("new", {"band": "hi"}),
            ("new", {"band": "mid"}),
            ("confirm", {}),
            ("sent", {}),
            ("viewed", {}),
            ("answer", {}),
        ]:
            listed = (await client.get(f"/api/board/column/{key}", params={"limit": 200, **params})).json()["items"]
            if name in _names(listed):
                return key
        return None

    vid = ids["suitable_today"]
    assert await where("suitable_today") == "new"
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": vid})
    assert r.status_code == 200, r.text
    app_id = r.json()["id"]
    assert await where("suitable_today") == "confirm"
    assert (await client.post(f"/api/applications/{app_id}/confirm", json={"sent": True})).json()["status"] == "sent"
    assert await where("suitable_today") == "sent"
    assert (await client.post(f"/api/applications/{app_id}/manual-update", json={"viewed": True})).status_code == 200
    assert await where("suitable_today") == "viewed"
    r = await client.post(f"/api/applications/{app_id}/manual-update", json={"state": "invitation"})
    assert r.status_code == 200, r.text
    assert await where("suitable_today") == "answer"

    # «Не отправил» brings an opened page back to «Новые»; skip and snooze take a card off the board
    other = ids["maybe"]
    opened = (await client.post("/api/applications/assist/open", json={"vacancy_id": other})).json()
    assert await where("maybe") == "confirm"
    await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": False})
    assert await where("maybe") == "new"
    await client.post(f"/api/vacancies/{other}/snooze", json={"days": 3})
    assert await where("maybe") is None
    board = (await client.get("/api/board")).json()
    assert board["new"]["snoozed"] == 2 and board["new"]["mid"] == 0
    await client.post(f"/api/vacancies/{ids['suitable_asks']}/skip", json={"reason": None})
    assert await where("suitable_asks") is None
