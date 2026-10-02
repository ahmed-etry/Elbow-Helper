"""CWL roster workbook output through the feature's analysis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...models import AgentAttachment
from ...actions.outcomes import CommandOutcome
from ..registry import CommandAdapter


async def run_cwl_roster(context: Any,
                         values: Mapping[str, Any]) -> CommandOutcome:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        return CommandOutcome.unavailable()
    requested_limit = int(values.get("history", 3))
    prepared = await workflow.prepare_cwl_roster(context.guild, requested_limit)
    if prepared["status"] != "complete":
        return CommandOutcome("complete", "private", text=prepared["issue"])
    try:
        filename, data = await workflow.build_cwl_roster_attachment(
            prepared["sheets"],
        )
    except (OSError, TypeError, ValueError):
        return CommandOutcome.unavailable()
    lines = workflow.cwl_roster_summary(
        prepared["history_label"], prepared["signed_member_count"],
        prepared["signed_account_count"],
    )
    return CommandOutcome(
        "complete", "private", text="\n".join(lines),
        attachments=(AgentAttachment(filename, data),),
    )


def cwl_roster_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/cwl roster", "private", run_cwl_roster),)
