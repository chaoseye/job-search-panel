"""Pydantic models for a few hh.ru payloads used in M0."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str | None = None
    expires_in: int = 1_209_600
    token_type: str = "bearer"


class MeInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    email: str | None = None
    phone: str | None = None
    is_applicant: bool = False
    is_employer: bool = False
    is_admin: bool = False
    is_application: bool = False
    auth_type: str | None = None
    counters: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or (self.email or "hh user")

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "is_applicant": self.is_applicant,
            "is_employer": self.is_employer,
            "auth_type": self.auth_type,
            "counters": self.counters,
        }
