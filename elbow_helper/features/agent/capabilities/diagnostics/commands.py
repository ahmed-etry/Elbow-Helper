"""Bot diagnostic command adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...actions.outcomes import CommandOutcome, embed_text
from ...commands.registry import CommandAdapter


async def run_api(context: Any, values: Mapping[str, Any]) -> CommandOutcome:
    workflow = context.bot.get_cog("DebugCog")
    if workflow is None:
        return CommandOutcome.unavailable()
    result = await workflow.check_clash_connection(values.get("clan"))
    if result.outcome == "complete" and result.embed is not None:
        return CommandOutcome("complete", text=embed_text(result.embed))
    if result.message:
        return CommandOutcome("complete", text=result.message)
    return CommandOutcome.unavailable()


def diagnostic_adapters() -> tuple[CommandAdapter, ...]:
    return (CommandAdapter("/api", "public", run_api),)
