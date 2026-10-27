"""Capability probe: checks which hh.ru methods the user's application can call."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from app.hh.client import HHClient, HHResponse
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHNegotiationError

log = structlog.get_logger("hh.probe")

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
SKIPPED = "skipped"


@dataclass
class ProbeStep:
    key: str
    title: str
    method: str
    path: str
    status: int | None = None
    capability: str = UNKNOWN
    note: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


@dataclass
class ProbeOptions:
    allow_publish: bool = False
    allow_blacklist: bool = False
    resume_id: str | None = None


@dataclass
class ProbeReport:
    started_at: str
    finished_at: str
    steps: list[ProbeStep]
    capabilities: dict[str, str]
    apply_mode_suggested: str
    resume_ids: list[dict[str, str]]
    me: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [asdict(s) for s in self.steps],
            "capabilities": self.capabilities,
            "apply_mode_suggested": self.apply_mode_suggested,
            "resume_ids": self.resume_ids,
            "me": self.me,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class _Runner:
    def __init__(self, client: HHClient) -> None:
        self.client = client
        self.steps: list[ProbeStep] = []

    async def call(
        self,
        step: ProbeStep,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        multipart: bool = False,
        classify: Callable[[HHResponse | HHError], tuple[str, str]] | None = None,
        auth: bool | str = True,
    ) -> HHResponse | HHError:
        self.steps.append(step)
        t0 = time.monotonic()
        result: HHResponse | HHError
        try:
            result = await self.client.request(
                step.method, step.path, params=params, data=data, multipart=multipart, auth=auth
            )
            step.status = result.status
        except HHError as exc:
            result = exc
            step.status = exc.status
            step.errors = exc.errors
            step.note = exc.summary()
        except Exception as exc:  # noqa: BLE001 - probe must never abort
            result = HHError(f"exception: {exc}")
            step.note = f"exception: {exc}"
        step.duration_ms = int((time.monotonic() - t0) * 1000)
        if classify is not None:
            step.capability, note = classify(result)
            if note:
                step.note = note
        else:
            step.capability, note = _classify_default(result)
            if note and not step.note:
                step.note = note
        log.info("probe_step", key=step.key, status=step.status, capability=step.capability, note=step.note)
        return result


def _classify_default(result: HHResponse | HHError) -> tuple[str, str]:
    if isinstance(result, HHResponse):
        return AVAILABLE, ""
    if isinstance(result, HHCaptchaRequired):
        return UNAVAILABLE, "hh требует капчу; пройдите её и повторите зонд"
    if isinstance(result, HHAuthError):
        return UNAVAILABLE, f"ошибка авторизации: {result.value}"
    if result.status == 404:
        return UNAVAILABLE, "404: метод отсутствует или недоступен"
    if result.status == 403:
        return UNAVAILABLE, f"403: {result.summary()}"
    if result.status == 400:
        return AVAILABLE, f"400 (метод есть, параметры отклонены): {result.summary()}"
    if result.status is None:
        return UNKNOWN, result.summary()
    return UNKNOWN, result.summary()


def _classify_negotiations_post(result: HHResponse | HHError) -> tuple[str, str]:
    if isinstance(result, HHResponse):
        return AVAILABLE, f"неожиданный успех {result.status}"
    if isinstance(result, HHNegotiationError):
        return AVAILABLE, f"метод отвечает ошибкой negotiations/{result.value} — отклик через API доступен"
    if result.status == 400:
        return AVAILABLE, f"400 — метод существует: {result.summary()}"
    if isinstance(result, HHCaptchaRequired):
        return UNKNOWN, "капча; пройдите и повторите"
    if result.status in (401, 403):
        return UNAVAILABLE, f"403 без типа negotiations — метод закрыт для приложения: {result.summary()}"
    if result.status == 404:
        return UNKNOWN, "404 — неоднозначно (нет вакансии или нет метода); нужен тест на реальной вакансии"
    return UNKNOWN, result.summary()


async def run_probe(client: HHClient, options: ProbeOptions | None = None) -> ProbeReport:
    options = options or ProbeOptions()
    runner = _Runner(client)
    started = _now()
    caps: dict[str, str] = {}
    me_public: dict[str, Any] | None = None
    resume_ids: list[dict[str, str]] = []
    vacancy_id: str | None = None

    # 1. /me
    r = await runner.call(ProbeStep("me", "Кто я", "GET", "/me"))
    if isinstance(r, HHResponse) and isinstance(r.data, dict):
        me_public = {
            "id": r.data.get("id"),
            "first_name": r.data.get("first_name"),
            "last_name": r.data.get("last_name"),
            "email": r.data.get("email"),
            "is_applicant": bool(r.data.get("is_applicant")),
            "is_employer": bool(r.data.get("is_employer")),
        }
        if not me_public["is_applicant"]:
            runner.steps[-1].capability = UNAVAILABLE
            runner.steps[-1].note = "токен не соискателя (is_applicant=false)"
    caps["me"] = runner.steps[-1].capability

    # 2. public dictionaries (token per HH_PUBLIC_AUTH — application token by default)
    public = client.public_auth
    token_note = "токен приложения" if public == "app" else "токен пользователя"
    await runner.call(ProbeStep("dictionaries", "Справочники", "GET", "/dictionaries"), auth=public)
    caps["dictionaries"] = runner.steps[-1].capability
    await runner.call(ProbeStep("areas", "Регионы", "GET", "/areas/113"), auth=public)
    caps["areas"] = runner.steps[-1].capability

    # 3. vacancy search: with the configured public token, plus an informational check with the user token
    r = await runner.call(
        ProbeStep("vacancies_search", f"Поиск вакансий ({token_note})", "GET", "/vacancies"),
        params={"text": "python", "per_page": 1, "order_by": "publication_time"},
        auth=public,
    )
    if isinstance(r, HHResponse) and isinstance(r.data, dict):
        items = r.data.get("items") or []
        if items and isinstance(items[0], dict) and items[0].get("id"):
            vacancy_id = str(items[0]["id"])
        runner.steps[-1].note = f"found={r.data.get('found')} ({token_note})"
    caps["vacancies_search"] = runner.steps[-1].capability
    other = "user" if public == "app" else "app"
    await runner.call(
        ProbeStep(
            "vacancies_search_user" if other == "user" else "vacancies_search_app",
            "Поиск вакансий (токен пользователя)" if other == "user" else "Поиск вакансий (токен приложения)",
            "GET",
            "/vacancies",
        ),
        params={"text": "python", "per_page": 1},
        auth=other,
    )
    caps[runner.steps[-1].key] = runner.steps[-1].capability

    # 4. vacancy details
    if vacancy_id:
        await runner.call(ProbeStep("vacancy_get", "Вакансия целиком", "GET", f"/vacancies/{vacancy_id}"), auth=public)
        caps["vacancy_get"] = runner.steps[-1].capability
        # 5. similar to vacancy
        await runner.call(
            ProbeStep("vacancy_similar", "Похожие на вакансию", "GET", f"/vacancies/{vacancy_id}/similar_vacancies"),
            params={"per_page": 1},
            auth=public,
        )
        caps["vacancy_similar"] = runner.steps[-1].capability
    else:
        for key, title in (("vacancy_get", "Вакансия целиком"), ("vacancy_similar", "Похожие на вакансию")):
            runner.steps.append(ProbeStep(key, title, "GET", "-", capability=SKIPPED, note="нет id вакансии"))
            caps[key] = SKIPPED

    # 6. my resumes
    r = await runner.call(ProbeStep("resumes_mine", "Мои резюме", "GET", "/resumes/mine"))
    if isinstance(r, HHResponse) and isinstance(r.data, dict):
        for item in r.data.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                resume_ids.append(
                    {
                        "id": str(item["id"]),
                        "title": str(item.get("title") or ""),
                        "status": str((item.get("status") or {}).get("id") or ""),
                    }
                )
        runner.steps[-1].note = f"резюме: {len(resume_ids)}"
    caps["resumes_mine"] = runner.steps[-1].capability

    resume_id = options.resume_id or (resume_ids[0]["id"] if resume_ids else None)

    # 7. resume details, 8. similar to resume
    if resume_id:
        await runner.call(ProbeStep("resume_get", "Резюме целиком", "GET", f"/resumes/{resume_id}"))
        caps["resume_get"] = runner.steps[-1].capability
        await runner.call(
            ProbeStep("resume_similar", "Вакансии под резюме", "GET", f"/resumes/{resume_id}/similar_vacancies"),
            params={"per_page": 1},
        )
        caps["resume_similar"] = runner.steps[-1].capability
    else:
        for key, title in (("resume_get", "Резюме целиком"), ("resume_similar", "Вакансии под резюме")):
            runner.steps.append(
                ProbeStep(key, title, "GET", "-", capability=SKIPPED, note="нет resume_id (укажите вручную)")
            )
            caps[key] = SKIPPED

    # 9. negotiations list
    await runner.call(
        ProbeStep("negotiations_list", "Список откликов", "GET", "/negotiations"),
        params={"status": "all", "per_page": 1},
    )
    caps["negotiations_list"] = runner.steps[-1].capability

    # 10. chats
    await runner.call(ProbeStep("chats", "Чаты", "GET", "/common/chats"))
    caps["chats"] = runner.steps[-1].capability

    # 11. safe check of POST /negotiations with a non-existent vacancy
    await runner.call(
        ProbeStep("negotiations_post", "Отклик через API (безопасная проверка)", "POST", "/negotiations"),
        data={"vacancy_id": "1", "resume_id": resume_id or "0"},
        multipart=True,
        classify=_classify_negotiations_post,
    )
    caps["negotiations_post"] = runner.steps[-1].capability

    # 12. publish (touch) resume - only with consent
    if options.allow_publish and resume_id:
        await runner.call(ProbeStep("resume_publish", "Поднять резюме", "POST", f"/resumes/{resume_id}/publish"))
        caps["resume_publish"] = runner.steps[-1].capability
    else:
        runner.steps.append(
            ProbeStep("resume_publish", "Поднять резюме", "POST", "-", capability=SKIPPED, note="не запрошено")
        )
        caps["resume_publish"] = SKIPPED

    # 13. blacklist put/delete - only with consent, reversible
    if options.allow_blacklist and vacancy_id:
        r = await runner.call(
            ProbeStep("vacancy_blacklist", "Скрыть вакансию (PUT)", "PUT", f"/vacancies/blacklisted/{vacancy_id}")
        )
        caps["vacancy_blacklist"] = runner.steps[-1].capability
        if isinstance(r, HHResponse):
            undo = ProbeStep(
                "vacancy_blacklist_undo", "Вернуть вакансию (DELETE)", "DELETE", f"/vacancies/blacklisted/{vacancy_id}"
            )
            await runner.call(undo)
    else:
        runner.steps.append(
            ProbeStep("vacancy_blacklist", "Скрыть вакансию", "PUT", "-", capability=SKIPPED, note="не запрошено")
        )
        caps["vacancy_blacklist"] = SKIPPED

    apply_mode = "api" if caps.get("negotiations_post") == AVAILABLE else "assist"
    return ProbeReport(
        started_at=started,
        finished_at=_now(),
        steps=runner.steps,
        capabilities=caps,
        apply_mode_suggested=apply_mode,
        resume_ids=resume_ids,
        me=me_public,
    )
