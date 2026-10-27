"""Packet and result schemas for the Claude Code exchange."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS = {1}
CLARIFY_MARKER_RE = re.compile(r"\[\[\s*УТОЧНИТЬ", re.IGNORECASE)
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@[\w.]+\.[a-z]{2,})", re.IGNORECASE)
CLICHE_PHRASES = (
    "стрессоустойчив",
    "командный игрок",
    "быстро обучаюсь",
    "ответственный",
    "хочу развиваться",
    "коммуникабельн",
    "нацелен на результат",
)


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


class EvalTags(BaseModel):
    """Short labels for analytics (guidelines v4): industry, what the job is about, tools."""

    model_config = ConfigDict(extra="ignore")

    domain: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    stack: list[str] = Field(default_factory=list)

    @field_validator("domain", "tasks", "stack", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return [s[:40] for s in _clean_list(v)][:6]


class Evaluation(BaseModel):
    """B.1"""

    model_config = ConfigDict(extra="ignore")

    score: int = Field(..., ge=0, le=100)
    verdict: Literal["apply", "maybe", "skip"]
    summary: str = ""
    matched: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    salary_fit: Literal["below", "within", "above", "unknown"] = "unknown"
    seniority_fit: Literal["under", "match", "over", "unknown"] = "unknown"
    language: Literal["ru", "en", "other"] = "ru"
    tags: EvalTags | None = None

    @field_validator("matched", "gaps", "red_flags", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else None

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return str(v or "").strip()


class Letter(BaseModel):
    """B.2"""

    model_config = ConfigDict(extra="ignore")

    text: str
    language: Literal["ru", "en", "other"] = "ru"
    mentions: list[str] = Field(default_factory=list)
    needs_clarification: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        text = str(v or "").replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("letter text is empty")
        return text

    @field_validator("mentions", "needs_clarification", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)

    def markers(self) -> list[str]:
        return [m.group(0) for m in re.finditer(r"\[\[\s*УТОЧНИТЬ[^\]]*\]\]", self.text, re.IGNORECASE)]

    def has_links(self) -> bool:
        return LINK_RE.search(self.text) is not None

    def cliches(self) -> list[str]:
        low = self.text.lower().replace("ё", "е")
        return [c for c in CLICHE_PHRASES if c in low]


class Reply(BaseModel):
    """B.3"""

    model_config = ConfigDict(extra="ignore")

    text: str
    intent: Literal["accept_invitation", "ask_details", "decline", "answer_question", "other"] = "other"
    needs_clarification: list[str] = Field(default_factory=list)


class PrepQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str
    answer: str = ""

    @field_validator("question", "answer", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return str(v or "").strip()


class InterviewPrepResult(BaseModel):
    """Task `interview_prep`: a study sheet before the interview."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    questions: list[PrepQuestion] = Field(default_factory=list)
    ask_them: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    prepare: list[str] = Field(default_factory=list)

    @field_validator("ask_them", "risks", "prepare", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, v: Any) -> list[Any]:
        return [q for q in (v if isinstance(v, list) else []) if isinstance(q, dict) and q.get("question")][:20]


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str
    answer: str
    needs_clarification: list[str] = Field(default_factory=list)

    @field_validator("question", "answer", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("needs_clarification", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)


class QuestionAnswersResult(BaseModel):
    """Task `answer_questions`: one answer per employer question."""

    model_config = ConfigDict(extra="ignore")

    answers: list[QuestionAnswer] = Field(default_factory=list)

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, v: Any) -> list[Any]:
        return [a for a in (v if isinstance(v, list) else []) if isinstance(a, dict) and a.get("answer")][:20]


class ResumeEdit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    section: str = ""
    proposed: str
    reason: str = ""

    @field_validator("section", "proposed", "reason", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return str(v or "").strip()


class ResumeAdviceResult(BaseModel):
    """Task `resume_advice`: concrete edits of the resume text for a vacancy segment."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    edits: list[ResumeEdit] = Field(default_factory=list)
    add_skills: list[str] = Field(default_factory=list)
    soften: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @field_validator("add_skills", "soften", "questions", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("edits", mode="before")
    @classmethod
    def _edits(cls, v: Any) -> list[Any]:
        return [e for e in (v if isinstance(v, list) else []) if isinstance(e, dict) and e.get("proposed")][:20]


PITCH_STYLES = ("business", "simple")
_STYLE_WORDS = {"business": "business", "деловой": "business", "formal": "business", "simple": "simple",
                "простой": "simple", "casual": "simple", "plain": "simple"}  # fmt: skip


class Pitch(BaseModel):
    """Task `studio_letter`: an e-mail to a studio — subject and body (the panel appends the user's signature) in one
    of the styles the task asked for (`styles`); an unknown style is read as business."""

    model_config = ConfigDict(extra="ignore")

    style: str = "business"
    subject: str
    text: str
    language: Literal["ru", "en", "other"] = "ru"
    needs_clarification: list[str] = Field(default_factory=list)

    @field_validator("style", mode="before")
    @classmethod
    def _style(cls, v: Any) -> str:
        return _STYLE_WORDS.get(str(v or "").strip().lower(), "business")

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, v: Any) -> str:
        subject = " ".join(str(v or "").split())
        if not subject:
            raise ValueError("pitch subject is empty")
        return subject[:200]

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        text = str(v or "").replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("pitch text is empty")
        return text

    @field_validator("needs_clarification", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)


class TenderBid(BaseModel):
    """Task `tender_reply`: the answer to a workspace.ru tender that the user posts on the site."""

    model_config = ConfigDict(extra="ignore")

    text: str
    language: Literal["ru", "en", "other"] = "ru"
    needs_clarification: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        text = str(v or "").replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("bid text is empty")
        return text

    @field_validator("needs_clarification", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _clean_list(v)


class ResultItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vacancy_id: str
    evaluation: Evaluation | None = None
    letter: Letter | None = None
    reply: Reply | None = None
    prep: InterviewPrepResult | None = None
    advice: ResumeAdviceResult | None = None
    answers: QuestionAnswersResult | None = None
    # studio_letter: a draft per style (guidelines v10); `pitch` — one business draft (v9 results)
    pitches: list[Pitch] | None = None
    pitch: Pitch | None = None
    bid: TenderBid | None = None
    error: str | None = None

    @field_validator("vacancy_id", mode="before")
    @classmethod
    def _vid(cls, v: Any) -> str:
        return str(v).strip()

    def studio_pitches(self) -> list[Pitch]:
        """The drafts of a studio letter, one per style (the first of a style wins), in PITCH_STYLES order."""
        seen: dict[str, Pitch] = {}
        for pitch in [*(self.pitches or []), *([self.pitch] if self.pitch else [])]:
            seen.setdefault(pitch.style, pitch)
        return [seen[s] for s in PITCH_STYLES if s in seen]


class ResultFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol_version: int = PROTOCOL_VERSION
    batch_id: str
    profile_version: int | None = None
    results: list[ResultItem] = Field(default_factory=list)


# ---- packet side ------------------------------------------------------------


class PacketVacancy(BaseModel):
    id: str
    name: str
    employer: dict[str, Any]
    area: str | None = None
    work_format: list[str] = Field(default_factory=list)
    employment_form: str | None = None
    experience: str | None = None
    experience_name: str | None = None
    salary: dict[str, Any] | None = None
    key_skills: list[str] = Field(default_factory=list)
    professional_roles: list[str] = Field(default_factory=list)
    responses_count: int | None = None
    published_at: str | None = None
    alternate_url: str | None = None
    response_letter_required: bool = False
    has_test: bool = False
    description: str = ""


class PacketTask(BaseModel):
    type: Literal[
        "evaluate_and_letter",
        "letter_revision",
        "reply_draft",
        "interview_prep",
        "resume_advice",
        "answer_questions",
        "studio_letter",
        "tender_reply",
    ]
    vacancy: PacketVacancy
    prescore: dict[str, Any] | None = None
    want_letter: bool = True
    previous_letter: str | None = None
    user_comment: str | None = None
    evaluation: dict[str, Any] | None = None
    thread: list[dict[str, Any]] | None = None
    user_intent: str | None = None
    # interview_prep: the letter that was sent with the application and the user's notes for the preparation
    letter_sent: str | None = None
    user_notes: str | None = None
    # resume_advice: the segment and what it demands (vacancy is a stub `advice-<id>`)
    segment: dict[str, Any] | None = None
    demand: dict[str, Any] | None = None
    # answer_questions: the employer's questions from the response form and the letter that goes with the response
    questions: list[str] | None = None
    current_letter: str | None = None
    # studio_letter: the studio (name, site, services, what it does, its vacancies) and the kind of letter —
    # «application» (a job application: the only kind for studios found through hh.ru) or «offer» (services)
    studio: dict[str, Any] | None = None
    letter_kind: str | None = None
    # studio_letter: the styles to write a draft in (business, simple — the user picks one)
    styles: list[str] | None = None
    # tender_reply: the workspace.ru tender (title, conditions, budget, deadline, description) and the user's rates
    tender: dict[str, Any] | None = None
    # read this profile instead of the packet's profile_file (a criteria set with its own resume)
    profile_file: str | None = None


class Packet(BaseModel):
    protocol_version: int = PROTOCOL_VERSION
    batch_id: str
    created_at: str
    profile_version: int | None
    profile_file: str = "profile.md"
    guidelines_file: str = "guidelines.md"
    user_notes: list[str] = Field(default_factory=list)
    letter_defaults: dict[str, Any] = Field(default_factory=dict)
    tasks: list[PacketTask] = Field(default_factory=list)
