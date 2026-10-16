"""Role registry. New roles (hustle, agent, ...) plug in here."""

from __future__ import annotations

from ..config import Settings
from .base import Role, Routine
from .mentor import make_mentor

__all__ = ["Role", "Routine", "build_role", "AVAILABLE_ROLES"]

AVAILABLE_ROLES = ("mentor",)


def build_role(name: str, settings: Settings) -> Role:
    if name == "mentor":
        return make_mentor(settings.morning_at, settings.evening_at)
    raise ValueError(f"Unknown role {name!r}. Available: {', '.join(AVAILABLE_ROLES)}")
