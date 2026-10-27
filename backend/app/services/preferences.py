"""Candidate preferences questionnaire, stored in the settings table under key `preferences`."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.state import get_setting, set_setting

PREFERENCES_KEY = "preferences"

WORK_FORMAT_LABELS = {
    "REMOTE": "удалённо",
    "HYBRID": "гибрид",
    "ON_SITE": "в офисе",
    "FIELD_WORK": "разъездная",
}
EMPLOYMENT_FORM_LABELS = {
    "FULL": "полная",
    "PART": "частичная",
    "PROJECT": "проект",
    "FLY_IN_FLY_OUT": "вахта",
    "SIDE_JOB": "подработка",
}
BUSINESS_TRIP_LABELS = {"never": "нет", "rarely": "редко", "ready": "готов"}


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        text = str(v).strip()
        if text and text not in out:
            out.append(text)
    return out


class LetterStyle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tone: str = "деловой, живой"
    min_chars: int = Field(600, ge=100, le=9000)
    max_chars: int = Field(1200, ge=200, le=10000)
    greeting: str = "Здравствуйте!"
    must_mention: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)

    @field_validator("must_mention", "avoid")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _clean_list(v)

    @field_validator("examples")
    @classmethod
    def _clean_examples(cls, v: list[str]) -> list[str]:
        return [str(x).strip() for x in v if str(x).strip()][:3]


class SkillWeight(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    weight: int = Field(3, ge=1, le=5)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_titles: list[str] = Field(default_factory=list)
    target_keywords: list[str] = Field(default_factory=list)
    stop_words_title: list[str] = Field(default_factory=list)
    stop_words_description: list[str] = Field(default_factory=list)
    salary_min: int | None = Field(None, ge=0)
    salary_currency: str = "RUR"
    salary_missing: Literal["keep", "skip"] = "keep"
    work_formats: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    employment_forms: list[str] = Field(default_factory=list)
    relocation: bool = False
    business_trips: Literal["never", "rarely", "ready"] = "rarely"
    excluded_employers: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    experience_min_years: float | None = Field(None, ge=0)
    experience_max_years: float | None = Field(None, ge=0)
    # the candidate's own total experience in years: compared with the vacancy requirement in the prescore when
    # set; otherwise the structured hh resume is used (a file resume gives none). Not a selection criterion.
    own_experience_years: float | None = Field(None, ge=0, le=60)
    languages: list[str] = Field(default_factory=list)
    no_agencies: bool = False
    accredited_it_only: bool = False
    wants: str = ""
    avoids: str = ""
    extra_facts: str = ""
    letter_style: LetterStyle = Field(default_factory=LetterStyle)
    skills_weights: list[SkillWeight] = Field(default_factory=list)

    @field_validator(
        "target_titles",
        "target_keywords",
        "stop_words_title",
        "stop_words_description",
        "areas",
        "excluded_employers",
        "excluded_industries",
        "languages",
    )
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return _clean_list(v)

    @field_validator("work_formats")
    @classmethod
    def _work_formats(cls, v: list[str]) -> list[str]:
        return [x for x in _clean_list([s.upper() for s in v]) if x in WORK_FORMAT_LABELS]

    @field_validator("employment_forms")
    @classmethod
    def _employment_forms(cls, v: list[str]) -> list[str]:
        return [x for x in _clean_list([s.upper() for s in v]) if x in EMPLOYMENT_FORM_LABELS]

    @field_validator("skills_weights")
    @classmethod
    def _skills(cls, v: list[SkillWeight]) -> list[SkillWeight]:
        seen: dict[str, SkillWeight] = {}
        for item in v:
            name = item.name.strip()
            if name and name.lower() not in seen:
                seen[name.lower()] = SkillWeight(name=name, weight=item.weight)
        return list(seen.values())

    @field_validator("salary_currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return (v or "RUR").strip().upper()[:3] or "RUR"

    @property
    def own_experience_months(self) -> int | None:
        return None if self.own_experience_years is None else int(round(self.own_experience_years * 12))

    def merged_skills(self, resume_skills: list[str]) -> list[SkillWeight]:
        """Weights from the questionnaire plus resume skills that have no weight yet (default 3)."""
        result = list(self.skills_weights)
        known = {s.name.lower() for s in result}
        for name in resume_skills:
            if name.lower() not in known:
                result.append(SkillWeight(name=name, weight=3))
                known.add(name.lower())
        return sorted(result, key=lambda s: (-s.weight, s.name.lower()))


# fields that drive the hard rules and the prescore (services/rules.py, services/prescore.py); everything else
# (wants/avoids/extra_facts/letter_style) describes the candidate and stays shared between criteria sets
CRITERIA_FIELDS: tuple[str, ...] = (
    "target_titles",
    "target_keywords",
    "stop_words_title",
    "stop_words_description",
    "salary_min",
    "salary_currency",
    "salary_missing",
    "work_formats",
    "areas",
    "employment_forms",
    "relocation",
    "business_trips",
    "excluded_employers",
    "excluded_industries",
    "experience_min_years",
    "experience_max_years",
    "languages",
    "no_agencies",
    "accredited_it_only",
    "skills_weights",
)


def criteria_of(prefs: Preferences) -> dict[str, Any]:
    """The selection-criteria part of a questionnaire (what a named set stores)."""
    return prefs.model_dump(include=set(CRITERIA_FIELDS))


def apply_criteria(base: Preferences, criteria: dict[str, Any]) -> Preferences:
    """Base questionnaire with the criteria fields replaced by a named set (validated like the questionnaire)."""
    parsed = Preferences.model_validate(
        {**criteria_of(base), **{k: v for k, v in criteria.items() if k in CRITERIA_FIELDS}}
    )
    return base.model_copy(update={k: getattr(parsed, k) for k in CRITERIA_FIELDS})


async def get_preferences(session: AsyncSession) -> Preferences:
    raw = await get_setting(session, PREFERENCES_KEY)
    if not isinstance(raw, dict):
        return Preferences()
    return Preferences.model_validate(raw)


async def save_preferences(session: AsyncSession, prefs: Preferences) -> None:
    await set_setting(session, PREFERENCES_KEY, prefs.model_dump())
