"""Summarize confirmed runs without exposing action details."""

from collections.abc import Mapping, Sequence
import json
from typing import Any

from ..wording import (
    ACTION_RUN_DONE, ACTION_RUN_NONE, ACTION_RUN_PARTIAL, ACTION_RUN_PERMISSION,
    ACTION_RUN_RESTARTED, ACTION_RUN_UNCONFIRMED,
)


def _labels(steps: Sequence[Mapping[str, Any]]) -> str:
    counts: dict[tuple[str, str], int] = {}
    for step in steps:
        key = (step["action_label"], step["action_class"])
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(label + (f" ({count})" if count > 1 else "")
                     for (label, _), count in counts.items())


def format_run_report(run: Mapping[str, Any]) -> str:
    completed = []
    unconfirmed = []
    not_done = []
    permissions: dict[str, list[Mapping[str, Any]]] = {}
    for step in run["steps"]:
        status = step["status"]
        if status == "completed":
            completed.append(step)
        elif status in ("uncertain", "interrupted", "running"):
            unconfirmed.append(step)
        elif status == "permission":
            outcome = json.loads(step.get("outcome_json") or "{}")
            permission = outcome.get("permission", "")
            if permission:
                permissions.setdefault(permission, []).append(step)
            else:
                not_done.append(step)
        else:
            not_done.append(step)
    lines = [ACTION_RUN_RESTARTED] if run.get("status") == "interrupted" else []
    if not_done:
        lines.append((ACTION_RUN_PARTIAL if completed else ACTION_RUN_NONE).format(
            finished=_labels(completed), labels=_labels(not_done),
        ))
    elif completed:
        lines.append(ACTION_RUN_DONE.format(finished=_labels(completed)))
    for permission, steps in permissions.items():
        lines.append(ACTION_RUN_PERMISSION.format(permission=permission, labels=_labels(steps)))
    if unconfirmed:
        lines.append(ACTION_RUN_UNCONFIRMED.format(labels=_labels(unconfirmed)))
    return "\n".join(lines)
