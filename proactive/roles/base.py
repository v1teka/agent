"""A role is a persona plus its routine of proactive check-ins, put on top of the shared core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class Routine:
    kind: str          # stable id, e.g. "morning"
    at: time           # local time of day
    instruction: str   # what the role should do when this check-in fires


@dataclass(frozen=True)
class Role:
    name: str
    persona: str
    onboarding: str
    routines: list[Routine] = field(default_factory=list)
