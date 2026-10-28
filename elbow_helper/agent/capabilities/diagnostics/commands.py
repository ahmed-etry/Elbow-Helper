"""Bot diagnostic command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...actions.outcomes import ActionOutcome, embed_text
from ...commands.registry import CommandAdapter


async def run_api(context: Any, values: Mapping[str, Any]) -> ActionOutcome:
    workflow = context.bot.get_cog("DebugCog")
    if workflow is None:
        return ActionOutcome.unavailable()
    result = await workflow.check_clash_connection(values.get("clan"))
    if result.outcome == "complete" and result.embed is not None:
        return ActionOutcome("complete", text=embed_text(result.embed))
    if result.message:
        return ActionOutcome("complete", text=result.message)
    return ActionOutcome.unavailable()


def diagnostic_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter(
        "/api", "public", run_api, capability_name="check_clash_api",
        usage_note=(
            "General Clash API status only. Check read or collector failures via "
            "query_bot_data (stored evidence) or read_clash (same kind and tags); answer from evidence."
        ),
    ),)
