"""Results produced by feature command adapters."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any, Mapping

import discord

from ..models import AgentAttachment
from ..wording import ACTION_EMPTY, ACTION_PRIVATE_NOTE, ACTION_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    status: str
    visibility: str = "public"
    text: str = ""
    private_parts: tuple[str, ...] = ()
    attachments: tuple[AgentAttachment, ...] = ()
    missing: tuple[str, ...] = ()
    missing_options: tuple[Mapping[str, Any], ...] = ()
    after: Any = None
    result: Mapping[str, Any] | None = None
    private_panel: Callable[[discord.Interaction], Awaitable[None]] | None = None
    command_name: str = ""
    posted_in: int | None = None

    @classmethod
    def needs_input(cls, descriptions: tuple[str, ...], *,
                    options: tuple[Mapping[str, Any], ...] = ()) -> "ActionOutcome":
        return cls("needs_input", missing=descriptions, missing_options=options)

    @classmethod
    def unavailable(cls) -> "ActionOutcome":
        return cls("unavailable")


def embed_text(embed: discord.Embed) -> str:
    """Keep a feature embed's visible facts in a plain agent reply."""
    parts = [str(value) for value in (embed.title, embed.description) if value]
    parts.extend(f"{field.name}: {field.value}" for field in embed.fields)
    if embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def command_reply(outcomes: list[ActionOutcome]) -> str:
    parts = [item.text for item in outcomes
             if item.status == "complete" and item.visibility == "public" and item.text]
    if any(item.visibility == "private" and (item.private_parts or item.attachments
                                              or item.private_panel)
           for item in outcomes):
        parts.append(ACTION_PRIVATE_NOTE)
    if parts:
        return "\n\n".join(parts)
    if any(item.status == "empty" for item in outcomes):
        return ACTION_EMPTY
    return ACTION_UNAVAILABLE
