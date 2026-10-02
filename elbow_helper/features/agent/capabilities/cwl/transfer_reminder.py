"""CWL transfer reminders through the CWL roster workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.discord_actions.safety import check_post_access

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_PREVIEW_BLANK,
    ACTION_TRANSFER_REMINDER_CHANNEL,
    ACTION_TRANSFER_REMINDER_CLEAR,
    ACTION_TRANSFER_REMINDER_DELETE,
    ACTION_TRANSFER_REMINDER_LABEL,
    ACTION_TRANSFER_REMINDER_PAGE,
)
from ...actions.outcomes import CommandOutcome
from ...commands.registry import CommandAdapter, PreparedCommandChange


async def prepare_transfer_reminder(
    context: Any, values: Mapping[str, Any],
) -> PreparedCommandChange | CommandOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError('CWL transfer reminders are unavailable.')
    exclude = values.get("exclude")
    prepared = await workflow.prepare_transfer_reminder(context.guild.id, exclude)
    if prepared["issue"]:
        raise ValueError(prepared["issue"])
    if prepared["status"] in ("unavailable", "no_change"):
        return CommandOutcome(
            "complete", "private", text="\n".join(prepared["result_lines"]),
        )
    channel = prepared["channel"]
    if channel is not None:
        check_post_access(channel, context.member, context.guild.me)
    lines = []
    if prepared["status"] == "clear":
        lines.append(ACTION_TRANSFER_REMINDER_CLEAR)
    else:
        lines.append(ACTION_TRANSFER_REMINDER_CHANNEL.format(channel=channel.mention))
    for entry in prepared["previous_entries"]:
        lines.append(ACTION_TRANSFER_REMINDER_DELETE.format(
            channel=f"<#{entry['channel_id']}>",
            message_id=entry["message_id"],
        ))
    for number, chunk in enumerate(prepared["chunks"], start=1):
        lines.append(ACTION_TRANSFER_REMINDER_PAGE.format(number=number))
        lines.extend(line or ACTION_PREVIEW_BLANK for line in chunk.splitlines())
    lines.extend(line or ACTION_PREVIEW_BLANK
                 for warning in prepared["result_lines"]
                 for line in warning.splitlines())

    def signature(item):
        return (
            item["status"], item["candidate_codes"], item["mismatches"],
            item["content"], item["chunks"], item["previous_entries"],
            item["result_lines"],
            item["channel"].id if item["channel"] else None,
        )

    initial = signature(prepared)

    async def recheck() -> bool:
        current = await workflow.prepare_transfer_reminder(context.guild.id, exclude)
        if current["issue"] or current["status"] not in ("clear", "post"):
            return False
        if current["channel"] is not None:
            try:
                check_post_access(current["channel"], context.member, context.guild.me)
            except ValueError:
                return False
        return signature(current) == initial

    async def run() -> CommandOutcome:
        result = await workflow.apply_transfer_reminder(
            prepared, enforce_state=True,
        )
        refs = workflow.transfer_reminder_state()
        if result["status"] == "complete":
            if prepared["status"] == "clear" and refs:
                raise OSError("Transfer reminder clear could not be verified")
            if prepared["status"] == "post" and {
                entry["message_id"] for entry in refs
            } != set(result["message_ids"]):
                raise OSError("Transfer reminder post could not be verified")
        return CommandOutcome(
            "complete", "private", text="\n".join(result["result_lines"]),
            result={"status": result["status"],
                    "message_ids": list(result["message_ids"]),
                    "channel_id": channel.id if channel else None},
            after={"status": result["status"],
                   "message_ids": list(result["message_ids"]),
                   "channel_id": channel.id if channel else None},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_TRANSFER_REMINDER_LABEL,
                      before={"previous_entries": prepared["previous_entries"]}),
        run,
    )


async def run_transfer_reminder(context: Any,
                                values: Mapping[str, Any]) -> CommandOutcome:
    prepared = await prepare_transfer_reminder(context, values)
    return await prepared.run() if isinstance(prepared, PreparedCommandChange) else prepared


def cwl_transfer_reminder_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter(
        "/transfer reminder", "confirm", run_transfer_reminder,
        prepare=prepare_transfer_reminder,
        action_class=ActionClass.CHANGE,
    ),)
