"""CWL roster announcements through the CWL feature."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.discord_actions.safety import check_post_access
from elbow_helper.features.cwl.announcements import PENDING_ROSTER_HUB_LINK

from ...actions.contracts import ActionClass, ChangePreview
from ...wording import (
    ACTION_CWL_ANNOUNCEMENT_CHANNEL,
    ACTION_CWL_ANNOUNCEMENT_CYCLE,
    ACTION_CWL_ANNOUNCEMENT_HUB,
    ACTION_CWL_ANNOUNCEMENT_LABEL,
    ACTION_CWL_ANNOUNCEMENT_LINK,
    ACTION_CWL_ANNOUNCEMENT_RELEASE,
    ACTION_CWL_ANNOUNCEMENT_PREVIEW_ONLY,
    ACTION_PREVIEW_BLANK,
)
from ...actions.outcomes import ActionOutcome
from ...commands.registry import CommandAdapter, PreparedCommandChange


def _options(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": values["deadline_mode"], "deadline": values["deadline"],
        "timezone_name": values["timezone"],
        "delayed_deadline": values.get("delayed_deadline"),
        "intro": values.get("intro"),
    }


async def prepare_roster_announcement(
    context: Any, values: Mapping[str, Any],
) -> PreparedCommandChange | ActionOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError('CWL roster announcements are unavailable.')
    preview_only = bool(values.get("preview", False))
    prepared = workflow.prepare_roster_announcement(
        **_options(values), require_hub=preview_only,
    )
    if prepared["issue"]:
        raise ValueError(prepared["issue"])
    if preview_only:
        return ActionOutcome(
            "complete", "private",
            text=prepared["content_preview"] + "\n" + ACTION_CWL_ANNOUNCEMENT_PREVIEW_ONLY,
        )
    channel = await workflow.resolve_roster_announcement_channel()
    if channel is None or channel.guild.id != context.guild.id:
        raise ValueError('CWL roster announcements are unavailable.')
    check_post_access(channel, context.member, context.guild.me)
    cycles = await workflow.roster_announcement_cycles(context.guild.id)
    if cycles is None:
        raise ValueError("CWL rosters aren't available, so the announcement wasn't posted.")
    names = await workflow.roster_announcement_roster_names(cycles)
    prior_release = workflow.roster_announcement_release_state()
    content = prepared["content_preview"].replace(
        f"]({PENDING_ROSTER_HUB_LINK})",
        f"] ({ACTION_CWL_ANNOUNCEMENT_LINK})",
    )
    lines = [ACTION_CWL_ANNOUNCEMENT_CHANNEL.format(channel=channel.mention)]
    if prepared["hub_url"] is None:
        lines.append(ACTION_CWL_ANNOUNCEMENT_HUB)
    lines.append(ACTION_CWL_ANNOUNCEMENT_RELEASE)
    lines.extend(ACTION_CWL_ANNOUNCEMENT_CYCLE.format(
        name=name,
    ) for name in names)
    details = tuple(line or ACTION_PREVIEW_BLANK for line in content.splitlines())

    async def recheck() -> bool:
        live_channel = await workflow.resolve_roster_announcement_channel()
        if live_channel is None or live_channel.id != channel.id:
            return False
        try:
            check_post_access(channel, context.member, context.guild.me)
        except ValueError:
            return False
        live = workflow.prepare_roster_announcement(
            **_options(values), require_hub=False,
        )
        if (live["issue"] or live["content_preview"] != prepared["content_preview"]
                or live["hub_url"] != prepared["hub_url"]):
            return False
        return (await workflow.roster_announcement_cycles(context.guild.id) == cycles
                and await workflow.roster_announcement_roster_names(cycles) == names
                and workflow.roster_announcement_release_state() == prior_release)

    async def run() -> ActionOutcome:
        result = await workflow.post_roster_announcement(prepared, channel)
        if result["issue"]:
            raise ValueError(result["issue"])
        messages = result["messages"]
        if not messages or not workflow.roster_announcement_released(cycles):
            raise OSError("CWL roster announcement could not be verified")
        return ActionOutcome(
            "complete", "private", text=result["message"],
            result={"channel_id": channel.id,
                    "message_ids": [message.id for message in messages]},
            after={"channel_id": channel.id,
                   "message_ids": [message.id for message in messages],
                   "released_cycles": cycles},
        )

    return PreparedCommandChange(
        ChangePreview(tuple(lines), recheck,
                      summary=ACTION_CWL_ANNOUNCEMENT_LABEL, details=details,
                      before={"released_cycles": prior_release}),
        run,
    )


async def run_roster_announcement(context: Any,
                                  values: Mapping[str, Any]) -> ActionOutcome:
    prepared = await prepare_roster_announcement(context, values)
    return await prepared.run() if isinstance(prepared, PreparedCommandChange) else prepared


def cwl_announcement_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter(
        "/roster announcement", "confirm", run_roster_announcement,
        prepare=prepare_roster_announcement,
        action_class=ActionClass.CHANGE,
    ),)
