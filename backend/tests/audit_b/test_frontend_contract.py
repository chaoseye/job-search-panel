"""Контракт фронтенда с бэкендом: каждый путь `/api/...`, встречающийся в frontend/src, должен существовать среди
маршрутов FastAPI (шаблонные `${…}` считаются параметром пути)."""

from __future__ import annotations

import re

import pytest

from app.config import ROOT_DIR
from app.main import app

SRC = ROOT_DIR / "frontend" / "src"
_LITERAL = re.compile(r"[\"'`](/api/[^\"'`]*)")
_TEMPLATE = re.compile(r"\$\{[^}]*\}")
VAR = "{*}"  # сегмент из `${…}` во фронтенде: подходит и под параметр пути, и под литерал (`${action}`)


def _frontend_paths() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for file in SRC.rglob("*.ts*"):
        text = file.read_text(encoding="utf-8")
        for m in _LITERAL.finditer(text):
            path = _TEMPLATE.sub(VAR, m.group(1)).split("?", 1)[0]
            found.setdefault(path, []).append(file.relative_to(ROOT_DIR).as_posix())
    return found


def _backend_paths() -> list[str]:
    return [p for p in app.openapi()["paths"] if p.startswith("/api/")]


def _segments_match(frontend: str, backend: str) -> bool:
    f, b = frontend.strip("/").split("/"), backend.strip("/").split("/")
    if len(f) != len(b):
        return False
    return all(fs == bs or fs == VAR or bs.startswith("{") for fs, bs in zip(f, b, strict=True))


@pytest.mark.skipif(not SRC.exists(), reason="frontend/src отсутствует")
def test_every_frontend_api_path_has_a_backend_route() -> None:
    backend = _backend_paths()
    paths = _frontend_paths()
    assert len(paths) >= 60, "ожидалось много вызовов API во фронтенде"
    unmatched: list[str] = []
    for path, files in sorted(paths.items()):
        candidate = path.rstrip("/")
        exact = any(_segments_match(candidate, bp) for bp in backend)
        prefix = path.endswith("/") and any(bp.startswith(candidate + "/") for bp in backend)
        if not exact and not prefix:
            unmatched.append(f"{path}  ← {', '.join(sorted(set(files)))}")
    assert unmatched == [], "пути фронтенда без маршрута на бэкенде:\n" + "\n".join(unmatched)


def test_backend_routes_used_by_the_frontend_cover_most_of_the_api() -> None:
    """Информационно: какие маршруты бэкенда фронтенд не вызывает (CLI/бот/скрипты — не дефект)."""
    frontend = list(_frontend_paths())
    unused = [bp for bp in _backend_paths() if not any(_segments_match(fp.rstrip("/"), bp) for fp in frontend)]
    # известные «не для интерфейса» маршруты: OAuth-колбэки, capture-капчи, экспорт CSV из CLI и т. п.
    assert len(unused) < 25, unused
