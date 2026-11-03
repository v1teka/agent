"""A role is a persona plus its routine of proactive check-ins, put on top of the shared core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class Routine:
    kind: str          # stable id, e.g. "morning"
    at: time           # local time of day
    instruction: str   # what the role should do when this check-in fires
    # The one check-in that keeps going, lighter, when the person stops answering.
    keep_when_quiet: bool = False
    # Fires only while the latest task is open, e.g. "how did it go?": once the person
    # has reported, there is nothing to ask.
    needs_open_task: bool = False


@dataclass(frozen=True)
class Role:
    name: str
    persona: str
    onboarding: str
    routines: list[Routine] = field(default_factory=list)

    def routine(self, kind: str) -> Routine | None:
        return next((r for r in self.routines if r.kind == kind), None)
