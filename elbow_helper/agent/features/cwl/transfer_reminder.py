"""CWL transfer reminders through the CWL roster workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.agent.discord_actions.safety import check_bot_post_access

from elbow_helper.agent.actions.contracts import ActionRefused, ActionClass, ChangePreview
from elbow_helper.agent.conversation.wording import ACTION_PREVIEW_BLANK, ACTION_TRANSFER_REMINDER_LABEL, ACTION_TRANSFER_REMINDER_PAGE
from elbow_helper.agent.actions.outcomes import ActionOutcome
from elbow_helper.agent.commands.registry import CommandAdapter, PreparedCommandChange


async def prepare_transfer_reminder(
    context: Any, values: Mapping[str, Any],
) -> PreparedCommandChange | ActionOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ActionRefused('CWL transfer reminders are unavailable.')
    exclude = values.get("exclude")
    prepared = await workflow.prepare_transfer_reminder(context.guild.id, exclude)
    if prepared["issue"]:
        raise ActionRefused(prepared["issue"])
    if prepared["status"] in ("unavailable", "no_change"):
        return ActionOutcome(
            "complete", "private", text="\n".join(prepared["result_lines"]),
        )
    channel = prepared["channel"]
    if channel is not None:
        check_bot_post_access(channel, context.guild.me)
    prior_channels = ", ".join(dict.fromkeys(f"<#{entry['channel_id']}>"
                                            for entry in prepared["previous_entries"]))
    if prepared["status"] == "clear":
        line = f"⏰ Clear the CWL transfer reminder from {prior_channels}"
    elif prior_channels:
        line = (f"⏰ Replace the prior CWL transfer reminder in {prior_channels} "
                f"with a post in {channel.mention}")
    else:
        line = f"⏰ Post the CWL transfer reminder in {channel.mention}"
    details = []
    for number, chunk in enumerate(prepared["chunks"], start=1):
        details.append(ACTION_TRANSFER_REMINDER_PAGE.format(number=number))
        details.extend(line or ACTION_PREVIEW_BLANK for line in chunk.splitlines())
    details.extend(line or ACTION_PREVIEW_BLANK
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
                check_bot_post_access(current["channel"], context.guild.me)
            except ValueError:
                return False
        return signature(current) == initial

    async def run() -> ActionOutcome:
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
        result_lines = result["result_lines"]
        if result["status"] == "complete":
            if prepared["status"] == "clear":
                result_lines = ("Cleared the CWL transfer reminder.", *result_lines)
            else:
                verb = "replaced" if prior_channels else "posted"
                result_lines = (f"CWL transfer reminder {verb} in {channel.mention}.", *result_lines[1:])
        return ActionOutcome(
            "complete", "private", text="\n".join(result_lines),
            result={"status": result["status"],
                    "message_ids": list(result["message_ids"]),
                    "channel_id": channel.id if channel else None},
            after={"status": result["status"],
                   "message_ids": list(result["message_ids"]),
                   "channel_id": channel.id if channel else None},
        )

    return PreparedCommandChange(
        ChangePreview((line,), recheck,
                      summary=ACTION_TRANSFER_REMINDER_LABEL, details=tuple(details),
                      before={"previous_entries": prepared["previous_entries"]}),
        run,
    )


async def run_transfer_reminder(context: Any,
                                values: Mapping[str, Any]) -> ActionOutcome:
    prepared = await prepare_transfer_reminder(context, values)
    return await prepared.run() if isinstance(prepared, PreparedCommandChange) else prepared


def cwl_transfer_reminder_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter(
        "/transfer reminder", "confirm", run_transfer_reminder,
        prepare=prepare_transfer_reminder,
        action_class=ActionClass.CHANGE,
        tool_description="Carry out /transfer reminder for an authorized requester to notify members who still need to move to their CWL clan.",
    ),)
