"""CWL brief command adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.agent.tools.discord_safety import check_post_access

from ...actions.contracts import ChangePreview
from ...wording import ACTION_CWL_BRIEF_LABEL, ACTION_CWL_BRIEF_LINE
from ...actions.outcomes import CommandOutcome
from ..registry import CommandAdapter


async def _brief(context: Any, values: Mapping[str, Any]):
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError("CWL briefs are unavailable")
    brief = await workflow.prepare_cwl_brief(
        clan=values["clan"], mode=values["mode"],
        helper_cwl=values["helper_cwl"], rotations=values["rotations"],
        lead_cwl=values.get("lead_cwl"), intro=values.get("intro"),
    )
    check_post_access(brief.channel, context.member, context.guild.me)
    return workflow, brief


async def prepare_cwl_brief(context: Any, values: Mapping[str, Any]) -> ChangePreview:
    workflow, brief = await _brief(context, values)

    async def recheck() -> bool:
        try:
            current, prepared = await _brief(context, values)
        except ValueError:
            return False
        return (current is workflow and prepared.channel.id == brief.channel.id
                and prepared.content == brief.content)

    return ChangePreview((
        ACTION_CWL_BRIEF_LINE.format(channel=brief.channel.mention),
        *brief.content.splitlines(),
    ), recheck, summary=ACTION_CWL_BRIEF_LABEL)


async def run_cwl_brief(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow, brief = await _brief(context, values)
    await workflow.post_cwl_brief(brief)
    return CommandOutcome("complete", result={"channel_id": brief.channel.id})


def cwl_brief_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/cwl brief", "confirm", run_cwl_brief,
                           prepare=prepare_cwl_brief),)
