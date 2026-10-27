"""Profile API: file upload, preferences, versions, hh import, primary/delete."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from tests.unit.test_resume_norm import FIXTURE


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _login(client: httpx.AsyncClient) -> None:
    respx.post("https://api.hh.ru/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100000})
    )
    respx.get("https://api.hh.ru/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})


async def test_empty_profile(api) -> None:
    client, _ = api
    body = (await client.get("/api/profile")).json()
    assert body["version"] is None and body["resumes"] == []
    assert any("Нет источников" in w for w in body["warnings"])
    assert (await client.get("/api/profile/md")).status_code == 404


async def test_file_upload_preferences_and_versions(api) -> None:
    client, runtime = api
    r = await client.post(
        "/api/profile/resumes/upload",
        files={"file": ("my cv.txt", "Иванов Иван\nPython, FastAPI\nОпыт 7 лет".encode(), "text/plain")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["resumes"][0]["source"] == "file" and body["resumes"][0]["is_primary"] is True
    assert body["file"]["name"] == "my cv.txt" and body["file"]["chars"] > 0
    md_path = runtime.settings.exchange_dir / "profile.md"
    assert md_path.exists()
    md = (await client.get("/api/profile/md")).text
    assert "profile_version: 1" in md and "Иванов Иван" in md
    assert list((runtime.settings.data_dir / "uploads").iterdir())[0].name.endswith("_my_cv.txt")

    # preferences change -> version 2
    prefs = body["preferences"]
    prefs["target_titles"] = ["Backend developer"]
    prefs["salary_min"] = 200000
    prefs["work_formats"] = ["REMOTE"]
    r = await client.put("/api/profile/preferences", json=prefs)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert "от 200 000 ₽" in (await client.get("/api/profile/md")).text

    # same preferences again -> still version 2 (hash unchanged)
    r = await client.put("/api/profile/preferences", json=prefs)
    assert r.json()["version"] == 2

    # force rebuild -> version 3
    r = await client.post("/api/profile/rebuild")
    assert r.json()["version"] == 3

    # invalid preferences rejected
    r = await client.put("/api/profile/preferences", json={"salary_min": -5})
    assert r.status_code == 422

    # second upload becomes primary; delete it -> the first one is primary again
    r = await client.post("/api/profile/resumes/upload", files={"file": ("cv2.md", b"# CV two", "text/markdown")})
    rows = r.json()["resumes"]
    assert [x["is_primary"] for x in rows] == [False, True]
    assert r.json()["version"] == 4
    r = await client.delete(f"/api/profile/resumes/{rows[1]['id']}")
    rows = r.json()["resumes"]
    assert len(rows) == 1 and rows[0]["is_primary"] is True
    assert r.json()["version"] == 5

    # delete the last source -> profile deactivated, md removed
    r = await client.delete(f"/api/profile/resumes/{rows[0]['id']}")
    assert r.json()["version"] is None
    assert not md_path.exists()


async def test_upload_rejects_bad_files(api) -> None:
    client, _ = api
    r = await client.post("/api/profile/resumes/upload", files={"file": ("cv.rtf", b"{\\rtf1}", "application/rtf")})
    assert r.status_code == 400 and "не поддерживается" in r.json()["detail"]
    r = await client.post("/api/profile/resumes/upload", files={"file": ("cv.txt", b"   ", "text/plain")})
    assert r.status_code == 400


async def test_hh_requires_login(api) -> None:
    client, _ = api
    assert (await client.post("/api/profile/hh/list")).status_code == 400
    assert (await client.post("/api/profile/hh/import", json={"resume_id": "abc"})).status_code == 400


@respx.mock
async def test_hh_import_flow(api) -> None:
    client, runtime = api
    await _login(client)
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rid = raw["id"]
    respx.get("https://api.hh.ru/resumes/mine").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": rid, "title": raw["title"], "status": {"id": "published"}, "updated_at": raw["updated_at"]}
                ]
            },
        )
    )
    respx.get(f"https://api.hh.ru/resumes/{rid}").mock(return_value=httpx.Response(200, json=raw))

    r = await client.post("/api/profile/hh/list")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["id"] == rid and r.json()["items"][0]["status"] == "published"

    r = await client.post("/api/profile/hh/import", json={"resume_id": f"https://hh.ru/resume/{rid}?from=x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    row = body["resumes"][0]
    assert row["source"] == "hh" and row["hh_resume_id"] == rid and row["is_primary"] is True
    assert row["title"] == raw["title"] and row["can_publish_or_update"] is True
    assert row["alternate_url"].endswith(rid)
    assert body["structured"]["skills"] == ["Python", "FastAPI", "PostgreSQL", "Docker"]
    assert body["manual_resume_id"] == rid
    assert body["capabilities"]["resumes_mine"] == "available" and body["capabilities"]["resume_get"] == "available"
    # resume skills appear in merged weights with default 3
    assert {"name": "Python", "weight": 3} in body["preferences"]["skills_weights"]
    md = (await client.get("/api/profile/md")).text
    assert "### Рога и копыта — Senior Python Developer" in md

    # re-import (refresh) with the same data -> no new version
    r = await client.post("/api/profile/hh/import", json={"resume_id": rid})
    assert r.json()["version"] == 1 and len(r.json()["resumes"]) == 1

    # add a file too -> version 2 combining both
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Полный текст".encode(), "text/plain")}
    )
    assert r.json()["version"] == 2
    md = (await client.get("/api/profile/md")).text
    assert "resume_id: " + rid in md and "## Полный текст резюме (из файла cv.txt)" in md


@respx.mock
async def test_hh_import_forbidden_marks_capability(api) -> None:
    client, _ = api
    await _login(client)
    respx.get("https://api.hh.ru/resumes/mine").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    r = await client.post("/api/profile/hh/list")
    assert r.status_code == 502 and "загрузите файл" in r.json()["detail"].lower()
    # capability recorded -> next call is refused before hitting hh (409)
    r = await client.post("/api/profile/hh/list")
    assert r.status_code == 409
    view = (await client.get("/api/profile")).json()
    assert view["capabilities"]["resumes_mine"] == "unavailable"
    assert any("недоступен по данным зонда" in w for w in view["warnings"])

    r = await client.post("/api/profile/hh/import", json={"resume_id": "not-hex!"})
    assert r.status_code == 400
