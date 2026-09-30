"""Results produced by feature command adapters."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Mapping

import discord

from ..models import AgentAttachment
from ..wording import COMMAND_EMPTY, COMMAND_MISSING_VALUE, COMMAND_PRIVATE_NOTE, COMMAND_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    status: str
    visibility: str = "public"
    text: str = ""
    private_parts: tuple[str, ...] = ()
    attachments: tuple[AgentAttachment, ...] = ()
    missing: tuple[str, ...] = ()
    after: Any = None
    result: Mapping[str, Any] | None = None
    private_panel: Callable[[discord.Interaction], Awaitable[None]] | None = None
    command_name: str = ""

    @classmethod
    def needs_input(cls, descriptions: tuple[str, ...]) -> "CommandOutcome":
        return cls("needs_input", missing=descriptions)

    @classmethod
    def unavailable(cls) -> "CommandOutcome":
        return cls("unavailable")


def command_reply(outcomes: list[CommandOutcome]) -> str:
    missing = tuple(dict.fromkeys(
        description for item in outcomes if item.status == "needs_input"
        for description in item.missing
    ))
    if missing:
        return COMMAND_MISSING_VALUE.format(
            values="\n".join(f"- {item}" for item in missing),
        )
    parts = [item.text for item in outcomes
             if item.status == "complete" and item.visibility == "public" and item.text]
    if any(item.visibility == "private" and (item.private_parts or item.attachments
                                              or item.private_panel)
           for item in outcomes):
        parts.append(COMMAND_PRIVATE_NOTE)
    if parts:
        return "\n\n".join(parts)
    if any(item.status == "empty" for item in outcomes):
        return COMMAND_EMPTY
    return COMMAND_UNAVAILABLE
