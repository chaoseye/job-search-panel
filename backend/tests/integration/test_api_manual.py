"""Manual journal of employer responses («Ассистент» mode without hh sync): the user records views, invitations,
rejections and messages; stage filters, statistics and reply drafts for Claude work on top of it."""

from __future__ import annotations

import json

import respx

from tests.integration.test_api_m4 import _consent, _seed, api  # noqa: F401

__all__ = ["api"]


async def _sent_assist_application(client, runtime, m) -> tuple[dict[str, int], int]:
    ids = await _seed(client, runtime, m, ["1", "2"])
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    await _consent(client)
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["1"]})
    assert r.status_code == 200, r.text
    app_id = r.json()["id"]
    r = await client.post(f"/api/applications/{app_id}/confirm", json={"sent": True})
    assert r.json()["status"] == "sent"
    return ids, app_id


@respx.mock(assert_all_called=False)
async def test_manual_journal_stages_messages_and_reply_draft(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, app_id = await _sent_assist_application(client, runtime, respx_mock)
    row = next(a for a in (await client.get("/api/applications")).json()["items"] if a["id"] == app_id)
    assert row["negotiation"] is None and row["hh_negotiation_id"] is None

    # nothing to record → 422; a not-sent application → 409
    assert (await client.post(f"/api/applications/{app_id}/manual-update", json={})).status_code == 422
    other = (await client.post("/api/applications/assist/open", json={"vacancy_id": ids["2"]})).json()
    r = await client.post(f"/api/applications/{other['id']}/manual-update", json={"viewed": True})
    assert r.status_code == 409

    # 1. viewed by the employer
    r = await client.post(f"/api/applications/{app_id}/manual-update", json={"viewed": True})
    assert r.status_code == 200, r.text
    neg = r.json()["application"]["negotiation"]
    assert (
        neg["manual"] is True and neg["viewed_by_opponent"] and neg["first_viewed_at"] and neg["state_id"] == "response"
    )
    assert r.json()["application"]["hh_negotiation_id"] == f"manual-{app_id}"
    assert r.json()["application"]["negotiation_url"] is None  # no hh page for a manual journal
    listing = (await client.get("/api/applications", params={"stage": "viewed"})).json()
    assert [a["id"] for a in listing["items"]] == [app_id] and listing["stage_counts"]["viewed"] == 1

    # 2. invitation with the employer's questions
    text = "Добрый день! Расскажите про опыт с 1С и когда готовы выйти?"
    r = await client.post(f"/api/applications/{app_id}/manual-update", json={"state": "invitation", "text": text})
    assert r.status_code == 200, r.text
    detail = r.json()["detail"]
    assert detail["negotiation"]["state_id"] == "invitation" and detail["negotiation"]["invited_at"]
    assert detail["negotiation"]["first_reaction_at"] and detail["negotiation"]["messages_count"] == 1
    assert [m["author_type"] for m in detail["messages"]] == ["employer"] and detail["messages"][0]["text"] == text
    assert [e["event_type"] for e in detail["events"]] == ["created", "viewed", "state_changed", "message"]
    assert (await client.get("/api/applications", params={"stage": "invitation"})).json()["total"] == 1
    assert (await client.get("/api/applications", params={"stage": "discard"})).json()["total"] == 0

    # the detail endpoint serves the manual negotiation; refreshing from hh is refused
    nid = f"manual-{app_id}"
    assert (await client.get(f"/api/negotiations/{nid}")).json()["negotiation"]["manual"] is True
    assert (await client.post(f"/api/negotiations/{nid}/refresh-messages")).status_code == 409
    assert (await client.post(f"/api/applications/{app_id}/withdraw")).status_code == 409

    # 3. a reply draft for Claude uses the recorded thread
    r = await client.post(
        f"/api/negotiations/{nid}/reply-draft", json={"user_intent": "ответить по 1С, выход через 2 недели"}
    )
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]
    assert batch_id
    packet = json.loads(
        (runtime.settings.exchange_dir / "pending" / f"batch_{batch_id}.json").read_text(encoding="utf-8")
    )
    task = next(t for t in packet["tasks"] if t["type"] == "reply_draft")
    assert task["thread"][0]["author"] == "employer" and task["thread"][0]["text"] == text
    assert task["user_intent"] == "ответить по 1С, выход через 2 недели"

    # 4. my own reply and a rejection later; statistics see the outcome
    r = await client.post(
        f"/api/applications/{app_id}/manual-update", json={"text": "Ответил по 1С", "author": "applicant"}
    )
    assert [m["author_type"] for m in r.json()["detail"]["messages"]] == ["employer", "applicant"]
    r = await client.post(f"/api/applications/{app_id}/manual-update", json={"state": "discard"})
    assert r.json()["application"]["negotiation"]["state_id"] == "discard"
    discard_detail = r.json()["detail"]
    assert (await client.get("/api/applications", params={"stage": "discard"})).json()["total"] == 1
    from app.services.stats import clear_cache

    clear_cache()
    stats = (await client.get("/api/stats", params={"period": "7d"})).json()
    assert stats["metrics"]["sent"] == 1 and stats["metrics"]["viewed"] == 1
    assert stats["metrics"]["invitations"] == 1 and stats["metrics"]["discards"] == 1

    # column sorts: the viewed/invited row ranks first by status and by "viewed", empty cells sink to the bottom
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["2"]})
    assert (await client.post(f"/api/applications/{r.json()['id']}/confirm", json={"sent": True})).status_code == 200

    async def order_of(**params: str) -> list[int]:
        return [a["id"] for a in (await client.get("/api/applications", params=params)).json()["items"]]

    assert (await order_of(sort="sent", order="desc"))[0] != app_id  # the newer application first
    assert (await order_of(sort="sent", order="asc"))[0] == app_id
    assert (await order_of(sort="viewed", order="desc"))[0] == app_id
    assert (await order_of(sort="viewed", order="asc"))[0] == app_id  # never-viewed rows stay last
    assert (await order_of(sort="status", order="asc"))[-1] == app_id  # discard ranks after a plain sent row
    assert (await order_of(sort="vacancy", order="asc"))[0] == app_id  # "Python developer 1" < "… 2"
    assert (await order_of(sort="bogus", order="up")) == await order_of(sort="sent", order="desc")

    # 5. a wrongly recorded message can be removed; hh-sourced messages cannot
    msg_id = discard_detail["messages"][1]["id"]
    r = await client.delete(f"/api/negotiations/{nid}/messages/{msg_id}")
    assert r.status_code == 200 and [m["author_type"] for m in r.json()["messages"]] == ["employer"]
    assert (await client.delete(f"/api/negotiations/{nid}/messages/999999")).status_code == 404

    # 6. the whole journal can be dropped: the application is back to "no hh data", stages and stats forget it
    r = await client.delete(f"/api/applications/{app_id}/manual-journal")
    assert r.status_code == 200 and r.json()["removed_messages"] == 1
    assert r.json()["application"]["hh_negotiation_id"] is None and r.json()["application"]["negotiation"] is None
    assert (await client.get(f"/api/negotiations/{nid}")).status_code == 404
    assert (await client.get("/api/applications", params={"stage": "discard"})).json()["total"] == 0
    assert (await client.delete(f"/api/applications/{app_id}/manual-journal")).status_code == 404


@respx.mock(assert_all_called=False)
async def test_daily_summary_reminds_about_unrecorded_applications(api, respx_mock: respx.MockRouter) -> None:
    """Without hh sync the evening summary lists sent applications that have no (fresh) journal record."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.base import utcnow
    from app.db.models import Application
    from app.services.notify import render_event
    from app.services.state import get_system_state, update_capability
    from app.services.stats import daily_summary_payload

    client, runtime = api
    ids, app_id = await _sent_assist_application(client, runtime, respx_mock)
    other = (await client.post("/api/applications/assist/open", json={"vacancy_id": ids["2"]})).json()
    assert (await client.post(f"/api/applications/{other['id']}/confirm", json={"sent": True})).status_code == 200
    async with runtime.session_factory() as session:
        for row in (await session.execute(select(Application))).scalars():
            row.sent_at = utcnow() - timedelta(days=9)
        await session.commit()
    # sync capability unknown → no reminder (hh would report the statuses itself)
    async with runtime.session_factory() as session:
        payload = await daily_summary_payload(session, runtime.settings)
    assert payload["journal"] == [] and payload["journal_total"] == 0
    # hh closed the method → both old applications are listed, oldest first
    async with runtime.session_factory() as session:
        await get_system_state(session)
        await update_capability(session, "negotiations_list", "unavailable")
        await session.commit()
    async with runtime.session_factory() as session:
        payload = await daily_summary_payload(session, runtime.settings)
    assert payload["journal_total"] == 2 and [j["application_id"] for j in payload["journal"]] == [app_id, other["id"]]
    assert payload["journal"][0]["days"] == 9
    text = render_event("daily", [payload], timezone=runtime.settings.timezone)
    assert "Без записи в журнале" in text and "Python developer 1" in text and "(9 дн.)" in text
    # a recorded outcome or a fresh note removes the application from the reminder
    assert (
        await client.post(f"/api/applications/{app_id}/manual-update", json={"state": "discard"})
    ).status_code == 200
    r = await client.post(f"/api/applications/{other['id']}/manual-update", json={"viewed": True})
    assert r.status_code == 200
    async with runtime.session_factory() as session:
        payload = await daily_summary_payload(session, runtime.settings)
    assert payload["journal_total"] == 0


@respx.mock(assert_all_called=False)
async def test_dropping_the_journal_detaches_the_interview_prep(api, respx_mock: respx.MockRouter) -> None:
    """A prep requested before any record creates the manual journal and keeps its row id. Dropping the journal
    forgets that id too: the next journal row may get the same id and would hand the prep someone else's thread."""
    from sqlalchemy import select

    from app.db.models import InterviewPrep

    client, runtime = api
    _, app_id = await _sent_assist_application(client, runtime, respx_mock)
    r = await client.post(f"/api/applications/{app_id}/interview-prep", json={"notes": "спросят про Kafka"})
    assert r.status_code == 200, r.text

    async def prep_negotiation() -> int | None:
        async with runtime.session_factory() as session:
            prep = (
                await session.execute(select(InterviewPrep).where(InterviewPrep.application_id == app_id))
            ).scalar_one()
            return prep.negotiation_id

    assert await prep_negotiation() is not None
    assert (await client.delete(f"/api/applications/{app_id}/manual-journal")).status_code == 200
    assert await prep_negotiation() is None
