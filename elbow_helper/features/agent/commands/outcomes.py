"""Results produced by feature command adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import AgentAttachment


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    status: str
    visibility: str = "public"
    text: str = ""
    private_parts: tuple[str, ...] = ()
    attachments: tuple[AgentAttachment, ...] = ()
    missing: str = ""

    @classmethod
    def needs_input(cls, name: str) -> "CommandOutcome":
        return cls("needs_input", missing=name)

    @classmethod
    def unavailable(cls) -> "CommandOutcome":
        return cls("unavailable")
