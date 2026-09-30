"""Typed proposals for confirmed changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..wording import ACTION_AUDIT_REASON, ACTION_PREVIEW_BLANK


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
    result_label: str = ""

    def __post_init__(self) -> None:
        if not self.lines or not callable(self.recheck) or self.count < 1:
            raise ValueError("A change needs preview lines and a precondition")
        for name in ("lines",):
            object.__setattr__(self, name, tuple(
                line if line.strip() else ACTION_PREVIEW_BLANK
                for line in getattr(self, name)
            ))


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


def earlier_result_label(actions: Sequence[PreparedAction],
                         reference: Mapping[str, Any]) -> str:
    """Describe a referenced action using its preview, never its plan step ID."""
    for action in reversed(actions):
        if action.step_id == reference.get("step"):
            return action.preview.result_label or action.preview.lines[0].strip()
    raise ValueError("The earlier action has no preview label")
