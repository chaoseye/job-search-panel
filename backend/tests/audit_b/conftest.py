"""Аудит 2026-09-23 (сессия «_b»): дополнительные тесты поверх основного набора.

Каталог самодостаточен: фикстура `settings` берётся из tests/conftest.py, property-тесты используют `hypothesis`
(если пакет не установлен в venv — они пропускаются, а не падают). Тесты, помеченные `xfail(strict=True)`,
документируют найденные дефекты: они «ожидаемо падают» на текущем коде и начнут падать «неожиданно», когда
дефект исправят, — тогда маркер нужно снять.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# hypothesis/coverage могут лежать вне venv (аудит ставил их в отдельный каталог); подхватываем через переменную
_extra = os.environ.get("AUDIT_PYLIBS")
if _extra and _extra not in sys.path and Path(_extra).exists():
    sys.path.insert(0, _extra)

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck  # noqa: E402
from hypothesis import settings as h_settings  # noqa: E402

h_settings.register_profile(
    "audit",
    max_examples=int(os.environ.get("AUDIT_MAX_EXAMPLES", "80")),
    deadline=None,
    database=None,  # без каталога .hypothesis/ в репозитории
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
h_settings.load_profile("audit")
