"""Bounded orchestration for the Core read-only agent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Mapping
import json
import logging
import sqlite3
from typing import Any

import discord

from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai import TextGenerationError

from .models import AgentRequestContext
from .access import AgentAccessLost, require_access
from .prompts import SYSTEM_PROMPT
from .prompts import build_request_prompt
from .tools import build_agent_tools


LOGGER = logging.getLogger(__name__)
MAX_MODEL_ROUNDS = 6
MAX_TOOL_CALLS = 12
MAX_TOOL_RESULT_CHARACTERS = 40_000
MAX_EVIDENCE_CHARACTERS = 150_000
TOOL_TIMEOUT_SECONDS = 30.0
AGENT_MAX_OUTPUT_TOKENS = 64_000


class AgentUnavailableError(RuntimeError):
    """Raised when a beta agent request cannot produce a final answer."""


class CoreAgentService:
    """Let the model choose among fixed tools until it can answer."""

    def __init__(self, model: AgentModel):
        self._model = model

    async def answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
    ) -> str:
        registry = build_agent_tools()
        session = self._model.create_agent_session(
            system_prompt=SYSTEM_PROMPT,
            prompt=build_request_prompt(
                question=question,
                local_context=local_context,
                guild_name=context.guild.name,
                asker_name=context.member.display_name,
                asked_at=context.source_message.created_at,
            ),
            tools=tuple(tool.definition for tool in registry.values()),
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")

        pending_results: tuple[AgentToolResult, ...] = ()
        total_tool_calls = 0
        evidence_characters = 0
        completed_lookups: set[str] = set()
        usage_totals = {
            "prompt": 0,
            "completion": 0,
            "cache_hit": 0,
            "cache_miss": 0,
        }

        try:
            for round_index in range(MAX_MODEL_ROUNDS):
                context = replace(context, member=require_access(
                    context.guild, context.member.id, context.source_message.channel,
                ))
                allow_tools = (
                    round_index < MAX_MODEL_ROUNDS - 1
                    and total_tool_calls < MAX_TOOL_CALLS
                    and evidence_characters < MAX_EVIDENCE_CHARACTERS
                )
                step = await session.advance(
                    pending_results,
                    allow_tools=allow_tools,
                )
                pending_results = ()
                usage_totals["prompt"] += step.usage.prompt_tokens
                usage_totals["completion"] += step.usage.completion_tokens
                usage_totals["cache_hit"] += step.usage.prompt_cache_hit_tokens
                usage_totals["cache_miss"] += step.usage.prompt_cache_miss_tokens

                if not step.tool_calls:
                    if step.content:
                        self._log_completion(
                            context=context,
                            rounds=round_index + 1,
                            tool_calls=total_tool_calls,
                            evidence_characters=evidence_characters,
                            usage=usage_totals,
                        )
                        return step.content
                    raise AgentUnavailableError("The agent returned no final answer")

                if not allow_tools:
                    raise AgentUnavailableError("The agent did not finish within its round limit")

                results: list[AgentToolResult] = []
                for call in step.tool_calls:
                    if total_tool_calls >= MAX_TOOL_CALLS:
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result(
                                    "The request has reached its tool-call limit. "
                                    "Answer from the evidence already collected."
                                ),
                            )
                        )
                        continue
                    total_tool_calls += 1
                    registered = registry.get(call.name)
                    if registered is None:
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result("That tool is not available."),
                            )
                        )
                        continue
                    arguments = _parse_arguments(call.arguments)
                    if arguments is None or not _valid_arguments(arguments, registered.definition.parameters):
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result("Arguments must match the tool's declared fields and types."),
                            )
                        )
                        continue
                    if evidence_characters >= MAX_EVIDENCE_CHARACTERS:
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result(
                                    "The request has reached its evidence limit. "
                                    "Answer from the evidence already collected."
                                ),
                            )
                        )
                        continue
                    context = replace(context, member=require_access(
                        context.guild, context.member.id, context.source_message.channel,
                    ))
                    cache_key = call.name + json.dumps(arguments, sort_keys=True)
                    if cache_key in completed_lookups:
                        results.append(AgentToolResult(call.call_id, _error_result(
                            "This identical lookup already ran. Use its earlier result."
                        )))
                        continue
                    content = await self._execute_tool(
                        name=call.name,
                        handler=registered.handler,
                        arguments=arguments,
                        context=context,
                    )
                    remaining = MAX_EVIDENCE_CHARACTERS - evidence_characters
                    content = _bound_tool_result(
                        content,
                        min(MAX_TOOL_RESULT_CHARACTERS, remaining),
                    )
                    evidence_characters += len(content)
                    completed_lookups.add(cache_key)
                    results.append(
                        AgentToolResult(call_id=call.call_id, content=content)
                    )
                pending_results = tuple(results)
        except TextGenerationError as error:
            raise AgentUnavailableError(str(error)) from error

        raise AgentUnavailableError("The agent did not finish")

    @staticmethod
    async def _execute_tool(
        *,
        name: str,
        handler: Any,
        arguments: Mapping[str, Any],
        context: AgentRequestContext,
    ) -> str:
        try:
            LOGGER.info("Agent lookup: tool=%s invoker=%s", name, context.member.id)
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
                payload = await handler(context, arguments)
            require_access(context.guild, context.member.id, context.source_message.channel)
            return json.dumps(payload, ensure_ascii=False, default=str)
        except AgentAccessLost:
            raise
        except asyncio.TimeoutError:
            LOGGER.warning(
                "Agent tool timed out: tool=%s invoker=%s",
                name,
                context.member.id,
            )
            return _error_result("That lookup timed out.")
        except (
            discord.DiscordException,
            KeyError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            LOGGER.exception(
                "Agent tool failed: tool=%s invoker=%s",
                name,
                context.member.id,
            )
            return _error_result("That lookup failed.")

    @staticmethod
    def _log_completion(
        *,
        context: AgentRequestContext,
        rounds: int,
        tool_calls: int,
        evidence_characters: int,
        usage: Mapping[str, int],
    ) -> None:
        LOGGER.info(
            "Agent completed: invoker=%s channel=%s rounds=%s tools=%s evidence_chars=%s "
            "prompt_tokens=%s completion_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s",
            context.member.id,
            context.source_message.channel.id,
            rounds,
            tool_calls,
            evidence_characters,
            usage["prompt"],
            usage["completion"],
            usage["cache_hit"],
            usage["cache_miss"],
        )


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_arguments(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """Validate the flat string/integer schemas used by the beta tools."""
    properties = schema.get("properties", {})
    if set(arguments) - set(properties) or set(schema.get("required", ())) - set(arguments):
        return False
    for name, value in arguments.items():
        field = properties[name]
        if field.get("type") == "string":
            if not isinstance(value, str) or not value.strip():
                return False
            if not field.get("minLength", 1) <= len(value) <= field.get("maxLength", 1024):
                return False
        elif field.get("type") == "integer":
            if type(value) is not int:
                return False
            if not field.get("minimum", 1) <= value <= field.get("maximum", 2**64 - 1):
                return False
        else:
            return False
    return True


def _bound_tool_result(content: str, limit: int) -> str:
    if limit < 2:
        return ""
    if len(content) <= limit:
        return content
    # JSON escaping can expand an excerpt; measure the encoded envelope itself.
    low, high = 0, len(content)
    result = "{}"
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps({"truncated": True, "result_excerpt": content[:midpoint]}, ensure_ascii=False)
        if len(candidate) <= limit:
            result = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return result


def _error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
