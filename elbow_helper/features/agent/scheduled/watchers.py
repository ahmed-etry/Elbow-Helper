"""Check current data before asking the model about a changed condition."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import json
import logging

import discord

from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import TextGenerationError

from ..engine.capability_contract import compile_capability_call
from ..engine.service import AgentService
from ..engine.service import AgentUnavailableError
from ..engine.registry import build_agent_tools
from ..prompts import WATCHER_SYSTEM_PROMPT
from ..text import chunk_response
from ..access import require_evidence_access
from ..disclosure import can_show
from ..prompts import (
    WATCHER_CONTINUATION_INSTRUCTION,
)

LOGGER = logging.getLogger(__name__)
WATCHER_OUTPUT_TOKENS = 2_000
WATCHER_MAX_RESULT_CHARACTERS = 64_000


async def read_current(context, reads):
    registry = build_agent_tools()
    results = []
    for read in reads:
        name = read["capability"]
        arguments = read["arguments"]
        tool = registry[name]
        scope = compile_capability_call(tool, arguments, contract=tool.contract)
        raw = await AgentService.execute_tool(
            name=name,
            handler=tool.handler,
            arguments=arguments,
            capability_scope=scope,
            context=context,
        )
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or payload.get("flags", {}).get("status") == "failed"
        ):
            raise ValueError("Watcher lookup failed")
        results.append({"capability": name, "result": comparison_data(payload)})
    encoded = json.dumps(results, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > WATCHER_MAX_RESULT_CHARACTERS:
        raise ValueError("Watcher results exceed their size limit")
    sources = await require_evidence_access(context)
    if not await can_show(context.source_message.channel, sources,
                          context.state.required_access, context.guild,
                          thread_members=context.disclosure_thread_members):
        raise ValueError("Watcher evidence cannot be shared in this channel")
    return results


def comparison_data(payload):
    """Drop lookup metadata while preserving source facts and their timestamps."""
    metadata = {"report_id", "observed_at", "ownership_observed_at", "retained_bytes"}
    return {key: value for key, value in payload.items() if key not in metadata}


async def evaluate(context, condition: str, results):
    prompt = json.dumps(
        {"condition": condition, "results": results}, ensure_ascii=False, default=str
    )
    session = context.bot.agent_model.create_agent_session(
        system_prompt=WATCHER_SYSTEM_PROMPT,
        prompt=prompt,
        tools=(),
        max_output_tokens=WATCHER_OUTPUT_TOKENS,
    )
    if session is None:
        raise AgentUnavailableError("The AI backend is not configured")
    content = ""
    for attempt in range(2):
        try:
            async with asyncio.timeout_at(context.deadline_monotonic):
                step = await session.advance(
                    allow_tools=False,
                    reasoning_effort=AgentReasoningEffort.LOW,
                    max_output_tokens=WATCHER_OUTPUT_TOKENS,
                    **(
                        {"continuation_instruction": WATCHER_CONTINUATION_INSTRUCTION}
                        if attempt
                        else {}
                    ),
                )
        except TextGenerationError as error:
            raise AgentUnavailableError("Watcher AI service is unavailable") from error
        content += step.content
        if not step.output_limit_reached:
            break
        LOGGER.warning(
            "Agent watcher model output limit reached: round=%s", attempt + 1
        )
    else:
        raise ValueError("Watcher decision ended at its output limit")
    if step.tool_calls:
        raise ValueError("Watcher decision requested an unexpected tool")
    try:
        content = content.strip()
        for fence in ("```json", "```"):
            if content.startswith(fence + "\n") and content.endswith("```"):
                content = content[len(fence):-3].strip()
                break
        result = json.loads(content)
    except (TypeError, ValueError) as error:
        raise ValueError("Watcher decision was incomplete") from error
    if (
        not isinstance(result, dict)
        or type(result.get("holds")) is not bool
        or not isinstance(result.get("alert"), str)
    ):
        raise ValueError("Watcher decision was invalid")
    return result["holds"], result["alert"].strip()


async def send_alert(context, alert: str):
    if not alert:
        raise ValueError("Watcher alert was empty")
    member = context.member
    for index, part in enumerate(chunk_response(f"{member.mention} {alert}")):
        await context.source_message.channel.send(
            part, allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[member] if index == 0 else [],
            ),
        )
