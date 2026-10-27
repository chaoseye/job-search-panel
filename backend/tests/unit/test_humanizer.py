"""humanizer-ru bridge: the vendored skill scanner flags AI-text markers in Russian letters (skipped without it)."""

from __future__ import annotations

import pytest

from app.services import humanizer

pytestmark = pytest.mark.skipif(not humanizer.available(), reason="skill .claude/skills/humanizer-ru not installed")

BAD = (
    "Здравствуйте! Стоит отметить, что в современном мире данная вакансия играет ключевую роль. "
    "Я не просто разработчик, а инженер, который раскроет потенциал вашей команды."
)
GOOD = (
    "Здравствуйте! Три года делаю сервисы на Python и FastAPI: последний проект сократил время ответа API "
    "с 800 до 120 мс. В вашей вакансии зацепила задача про очереди на Kafka, с ней я работал в двух командах. "
    "Готов рассказать подробнее на созвоне."
)


def test_is_russian() -> None:
    assert humanizer.is_russian(BAD) and not humanizer.is_russian("Hello, I am a Python developer.")


async def test_scan_flags_hard_bans_and_scores() -> None:
    bad = await humanizer.scan_text(BAD)
    if bad is None:
        pytest.skip("scanner deps (razdel, pymorphy3) not installed")
    assert bad["hard_bans"] and bad["score"] < humanizer.MIN_SCORE
    assert any("Не просто" in b for b in bad["hard_bans"])
    warnings = humanizer.warnings_for(bad)
    assert warnings[0].startswith("humanizer: жёсткие запреты") and warnings[1].startswith("humanizer: чистота")
    good = await humanizer.scan_text(GOOD)
    assert good is not None and not good["hard_bans"] and good["score"] > bad["score"]
    assert await humanizer.letter_warnings("Hello, plain English text.") == []


async def test_letter_check_endpoint_and_import_warnings(settings) -> None:
    import httpx
    from httpx import ASGITransport

    from app.main import app
    from app.runtime import Runtime

    runtime = await Runtime.create(settings.model_copy(update={"humanizer_check": True}))
    app.state.runtime = runtime
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/vacancies/letter-check", json={"text": BAD})
            if r.status_code == 503:
                pytest.skip("scanner deps not installed")
            assert r.status_code == 200, r.text
            assert r.json()["result"]["hard_bans"] and r.json()["warnings"]
            r = await client.post("/api/vacancies/letter-check", json={"text": "Plain English."})
            assert r.json()["skipped"] and r.json()["warnings"] == []
        # an edited letter gets the same warnings
        from app.db.base import utcnow
        from app.db.models import Vacancy
        from app.services.letters import add_letter

        async with runtime.session_factory() as session:
            v = Vacancy(hh_id="h1", name="Python dev", pipeline_status="suitable", first_seen_at=utcnow())
            session.add(v)
            await session.flush()
            row = await add_letter(session, v, text=BAD, origin="claude", check_style=True)
            assert any(w.startswith("humanizer:") for w in __import__("json").loads(row.warnings_json))
            row2 = await add_letter(session, v, text=BAD, origin="template", check_style=True)
            assert not any(w.startswith("humanizer:") for w in __import__("json").loads(row2.warnings_json))
    finally:
        await runtime.close()
