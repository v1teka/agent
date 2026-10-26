"""LLM layer. The core talks to `LLM.think()` and never to a vendor SDK directly,
so a local model or another provider can be plugged in later."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

FactCategory = Literal[
    "biography", "education", "work", "income", "health", "habits",
    "interests", "relationships", "goals", "character", "preferences", "other",
]


class NewFact(BaseModel):
    category: FactCategory
    text: str = Field(description="One short fact about the person, in the person's language.")


class NewTask(BaseModel):
    title: str = Field(description="Short, concrete, doable today.")
    details: str = Field(description="How to do it and why it matters for this person.")


class TaskUpdate(BaseModel):
    task_id: int
    status: Literal["done", "skipped"]
    note: str = Field(description="What happened, in a few words.")


class FollowUp(BaseModel):
    in_minutes: int = Field(description="When to write to the person next, in minutes from now (15..4320).")
    reason: str = Field(description="What to check or say at that moment.")


class Turn(BaseModel):
    """Everything the model decides in one step."""

    message: str = Field(
        description="Text to send to the person now. Empty string means: say nothing this time."
    )
    new_facts: list[NewFact] = Field(description="Facts learned in this exchange that are not yet in memory.")
    new_task: NewTask | None = Field(description="A new task, only when you are actually assigning one.")
    task_updates: list[TaskUpdate] = Field(description="Status changes of existing tasks.")
    follow_ups: list[FollowUp] = Field(
        description="Your own proactive check-ins to schedule, at most 2. Empty if the routine schedule is enough."
    )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


class ThinkError(RuntimeError):
    """The model answered but gave no usable output; tokens were still spent."""

    def __init__(self, message: str, usage: Usage):
        super().__init__(message)
        self.usage = usage


SILENT = Turn(message="", new_facts=[], new_task=None, task_updates=[], follow_ups=[])


class LLM(Protocol):
    async def think(self, system: str, messages: list[dict]) -> tuple[Turn, Usage]: ...


class ClaudeLLM:
    def __init__(self, model: str, effort: str = "low"):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort

    async def think(self, system: str, messages: list[dict]) -> tuple[Turn, Usage]:
        response = await self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_format=Turn,
            output_config={"effort": self.effort},
            # If a safety classifier declines, retry server-side on a recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read=u.cache_read_input_tokens or 0,
            cache_write=u.cache_creation_input_tokens or 0,
        )
        if response.stop_reason == "refusal":
            log.warning("Model refused: %s", response.stop_details)
            return SILENT, usage
        if response.parsed_output is None:
            raise ThinkError(f"No structured output (stop_reason={response.stop_reason})", usage)
        return response.parsed_output, usage
