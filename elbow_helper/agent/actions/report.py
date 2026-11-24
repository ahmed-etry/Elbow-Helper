"""Summarize confirmed runs without exposing action details."""

from collections.abc import Mapping, Sequence
import json
from typing import Any

import discord

from elbow_helper.agent.conversation.wording import (
    ACTION_CONFIRM_DONE, ACTION_CONFIRM_PARTLY_DONE,
    ACTION_RUN_DONE, ACTION_RUN_NONE, ACTION_RUN_PARTIAL, ACTION_RUN_PERMISSION,
    ACTION_RUN_RESTARTED, ACTION_RUN_UNCONFIRMED, ACTION_RUN_NOT_DONE, ACTION_UNAVAILABLE,
)


def _labels(steps: Sequence[Mapping[str, Any]]) -> str:
    counts: dict[tuple[str, str], int] = {}
    for step in steps:
        key = (step["action_label"], step["action_class"])
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(label + (f" ({count})" if count > 1 else "")
                     for (label, _), count in counts.items())


def _change_results(steps):
    for step in steps:
        outcome = json.loads(step.get("outcome_json") or "{}")
        recipients = (outcome.get("result") or {}).get("direct_messages")
        if step["status"] == "completed" and isinstance(recipients, list):
            for recipient in recipients:
                yield {**step, "action_label": recipient["label"],
                       "dm_member": recipient.get("member"),
                       "status": "completed" if recipient["delivered"] else "failed"}, outcome
        else:
            yield step, outcome


def format_card_report(steps):
    done = failed = False
    statuses = []
    lines = []
    for step, outcome in _change_results(steps):
        label = step["action_label"]
        status = step["status"]
        if status == "completed":
            done = True
            statuses.append("✓ " + ACTION_RUN_DONE.format(finished=label))
            if step.get("dm_member") is not None:
                lines.append(f"✓ DM sent to {step['dm_member']}.")
            elif outcome.get("visibility") == "public" and outcome.get("text"):
                lines.append("✓ " + outcome["text"])
            else:
                lines.append(statuses[-1])
        else:
            failed = True
            permission = outcome.get("permission")
            if status == "permission" and permission:
                reason = ACTION_RUN_PERMISSION.format(permission=permission, labels=label)
            elif status in ("uncertain", "interrupted", "running"):
                reason = ACTION_RUN_UNCONFIRMED.format(labels=label)
            else:
                reason = ACTION_RUN_NOT_DONE.format(labels=label)
            statuses.append("✗ " + reason)
            lines.append(statuses[-1])
    title = (ACTION_CONFIRM_PARTLY_DONE if failed else ACTION_CONFIRM_DONE) if done else None
    card = discord.Embed(title=title, colour=discord.Colour.green() if done and not failed
                         else discord.Colour.red())
    report = "\n".join(lines)
    if len(report) <= 4096:
        card.description = report
        return [card], None
    if len(card) + len(report) <= 6000 and all(len(line) <= 4096 for line in lines):
        description = ""
        cards = [card]
        for line in lines:
            if len(description) + len(line) + 1 > 4096:
                cards[-1].description = description
                cards.append(discord.Embed(colour=card.colour))
                description = ""
            description = "\n".join(filter(None, (description, line)))
        cards[-1].description = description
        return cards, None
    summary = "\n".join(statuses)
    if len(summary) <= 4096:
        card.description = summary
    elif not title:
        card.description = ACTION_UNAVAILABLE
    return [card], report


def format_run_report(run: Mapping[str, Any]) -> str:
    completed = []
    unconfirmed = []
    not_done = []
    permissions: dict[str, list[Mapping[str, Any]]] = {}
    for step, outcome in _change_results(run["steps"]):
        status = step["status"]
        if status == "completed":
            completed.append(step)
        elif status in ("uncertain", "interrupted", "running"):
            unconfirmed.append(step)
        elif status == "permission":
            permission = outcome.get("permission", "")
            if permission:
                permissions.setdefault(permission, []).append(step)
            else:
                not_done.append(step)
        else:
            not_done.append(step)
    lines = [ACTION_RUN_RESTARTED] if run.get("status") == "interrupted" else []
    details = any(
        outcome.get("visibility") == "public" and outcome.get("text")
        for step in completed for outcome in [json.loads(step.get("outcome_json") or "{}")]
    )
    if details:
        for step in completed:
            lines.append(ACTION_RUN_DONE.format(finished=_labels([step])))
            outcome = json.loads(step.get("outcome_json") or "{}")
            if outcome.get("visibility") == "public" and outcome.get("text"):
                lines.append(outcome["text"])
        if not_done:
            lines.append(ACTION_RUN_NOT_DONE.format(labels=_labels(not_done)))
    elif not_done:
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
