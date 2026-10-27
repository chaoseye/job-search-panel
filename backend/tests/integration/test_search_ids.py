"""The id of a deleted search is never given out again (migration 0027, AUTOINCREMENT): vacancies keep the ids of
the searches that found them, and a new search must not inherit them in the filter, the statistics or a rebind to
its criteria set."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import SavedSearch, Vacancy
from app.db.session import alembic_config
from tests.integration.test_api_m2 import _create_search, api  # noqa: F401

__all__ = ["api"]


async def test_a_new_search_never_takes_the_id_of_a_deleted_one(api) -> None:
    client, runtime = api
    await _create_search(client)
    last = await _create_search(client, text="golang")
    async with runtime.session_factory() as session:
        session.add(Vacancy(hh_id="777", name="Go developer", source_search_ids=f",{last['id']},"))
        await session.commit()
    assert (await client.delete(f"/api/searches/{last['id']}")).json() == {"ok": True}

    new = await _create_search(client, text="rust")
    assert new["id"] > last["id"]
    # the vacancy found by the deleted search is not the new search's: not in its filter ...
    listing = (await client.get("/api/vacancies", params={"group": "all", "search_id": new["id"]})).json()
    assert listing["items"] == []
    old = (await client.get("/api/vacancies", params={"group": "all", "search_id": last["id"]})).json()
    assert [v["hh_id"] for v in old["items"]] == ["777"]
    # ... and not moved when the new search gets a criteria set
    pid = (await client.post("/api/selection-profiles", json={"name": "Rust"})).json()["id"]
    r = await client.put(
        f"/api/searches/{new['id']}",
        json={"name": "Rust", "params": {"text": "rust"}, "interval_min": 30, "selection_profile_id": pid},
    )
    assert r.status_code == 200, r.text
    async with runtime.session_factory() as session:
        vacancy = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "777"))).scalar_one()
        assert vacancy.selection_profile_id is None


def _table_sql(con: sqlite3.Connection) -> str:
    return str(con.execute("SELECT sql FROM sqlite_master WHERE name = 'saved_searches'").fetchone()[0])


def _counter(con: sqlite3.Connection) -> int | None:
    has_table = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone()
    row = con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'saved_searches'").fetchone() if has_table else None
    return int(row[0]) if row else None


def _rows(con: sqlite3.Connection) -> list[tuple[object, ...]]:
    return con.execute("SELECT * FROM saved_searches ORDER BY id").fetchall()


def _add_search(settings: Settings, name: str) -> int:
    engine = create_engine(settings.sync_db_url)
    try:
        with Session(engine) as session:
            row = SavedSearch(name=name, params_json='{"text": "rust"}')
            session.add(row)
            session.commit()
            return row.id
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("references", "counter"),
    [
        ([",1,7,", ",3,"], 7),  # search 7 was deleted, its vacancies still name it
        ([",2,"], 3),
        ([], 3),
    ],
)
def test_migration_keeps_the_searches_and_starts_past_every_referenced_id(
    tmp_path: Path, references: list[str], counter: int
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    command.upgrade(cfg, "0026_ws_tenders")
    engine = create_engine(settings.sync_db_url)
    with Session(engine) as session:
        for i in (1, 2, 3):
            session.add(
                SavedSearch(
                    id=i,
                    name=f"Поиск {i}",
                    params_json='{"text": "python"}',
                    interval_min=15 * i,
                    enabled=i != 2,
                    watch_only=i == 3,
                    selection_profile_id=5 if i == 1 else None,
                    last_found=10 * i,
                    last_error="HTTP 500" if i == 2 else None,
                )
            )
        for n, ids in enumerate(references):
            session.add(Vacancy(hh_id=str(100 + n), name=f"Вакансия {n}", source_search_ids=ids))
        session.commit()
    engine.dispose()
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    assert "AUTOINCREMENT" not in _table_sql(con).upper()
    before = _rows(con)
    con.close()

    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    assert "AUTOINCREMENT" in _table_sql(con).upper()
    assert _rows(con) == before
    assert _counter(con) == counter
    con.close()
    assert _add_search(settings, "Новый") == counter + 1

    # down and up again: the rows stay, the counter goes with the keyword and comes back past the highest id
    command.downgrade(cfg, "0026_ws_tenders")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    assert "AUTOINCREMENT" not in _table_sql(con).upper()
    assert _rows(con)[:3] == before and len(_rows(con)) == 4
    assert _counter(con) is None
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    assert _counter(con) == counter + 1
    con.close()
