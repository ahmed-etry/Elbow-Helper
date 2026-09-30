"""Typed proposals for confirmed changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..wording import ACTION_AUDIT_REASON


class ActionClass(StrEnum):
    READ = "read"
    OUTPUT = "output"
    CHANGE = "change"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True, slots=True)
class ChangePreview:
    lines: tuple[str, ...]
    recheck: Callable[[], Awaitable[bool]]
    summary: str = ""
    count: int = 1
    before: Any = None

    def __post_init__(self) -> None:
        if not self.lines or not callable(self.recheck) or self.count < 1:
            raise ValueError("A change needs preview lines and a precondition")


@dataclass(frozen=True, slots=True)
class PreparedAction:
    path: str
    values: Mapping[str, Any]
    preview: ChangePreview
    run: Callable[[], Awaitable[Any]]
    action_class: ActionClass = ActionClass.CHANGE
    verify: Callable[[], Awaitable[bool | None]] | None = None
    undo: Callable[[], Awaitable[Any]] | None = None
    permission: str = ""
    step_id: str = ""
    bind: Callable[[Mapping[str, Mapping[str, Any]]], Awaitable["PreparedAction"]] | None = None

    def __post_init__(self) -> None:
        if self.action_class not in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
            raise ValueError("Only changes may be prepared for confirmation")
        if not callable(self.run):
            raise ValueError("A change needs a run handler")


def check_bundle(actions: tuple[PreparedAction, ...]) -> None:
    if not actions:
        raise ValueError("No changes were prepared")
    if len(actions) > 1 and any(
        action.action_class is ActionClass.IRREVERSIBLE for action in actions
    ):
        raise ValueError("An irreversible change needs its own confirmation")


def audit_reason(member: Any) -> str:
    return ACTION_AUDIT_REASON.format(
        member=str(getattr(member, "display_name", member))[:80],
        member_id=member.id,
    )[:512]
