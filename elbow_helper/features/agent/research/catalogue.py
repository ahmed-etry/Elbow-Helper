"""Model-facing capability definitions for Discord reads and actions."""

from __future__ import annotations
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .history import read_discord_channel_history, find_discord_channels, read_message_context
from .search import search_discord_messages
from .history import (
    SEARCH_RESULT_LIMIT, INTERACTIVE_HISTORY_PAGE_LIMIT, HISTORY_PAGE_LIMIT, CONTEXT_MESSAGE_LIMIT,
)


def discord_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_channels",
                description="Find accessible Discord channels by name or ID.",
                parameters={
                    "type": "object", "properties": {"query": {"type": "string", "maxLength": 100}},
                    "required": ["query"], "additionalProperties": False,
                },
            ),
            find_discord_channels,
            contract=CapabilityContract(entity_fields=(), source_scope="channel_locator"),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="search_discord_messages",
                description=(
                    "Search accessible Discord messages by author, mentions, words, channels or "
                    "dates."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 0,
                            "maxLength": 1024,
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": SEARCH_RESULT_LIMIT,
                            "default": 5,
                        },
                        "channel_id": {"type": "integer", "minimum": 1},
                        "channel_ids": {
                            "type": "array", "minItems": 1, "maxItems": 20,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "cursor": {
                            "type": "string", "maxLength": 64,
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "mentions": {"type": "array", "minItems": 1, "maxItems": 20,
                                     "items": {"type": "integer", "minimum": 1}},
                        "after": {"type": "string", "maxLength": 40},
                        "before": {"type": "string", "maxLength": 40},
                    },
                    "additionalProperties": False,
                },
                usage_note=(
                    "Search misses embeds; filter history for bot logs. Omit query for author "
                    "history; respect scope, continue cursors. ISO dates: after inclusive, "
                    "before exclusive."
                ),
            ),
            search_discord_messages,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("channel_ids", "discord_channel_set"),
                    ("author_id", "discord_member"),
                    ("mentions", "discord_member_set"),
                ),
                source_scope="channel_messages",
                channel_fields=("channel_id", "channel_ids"),
                result_channel_lists=(("matches", "channel_id"),),
                result_sources_within_query=True,
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_channel_history",
                description=(
                    "Read channel history from the required after date to an optional before date."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "integer", "minimum": 1},
                        "after": {
                            "type": "string", "maxLength": 40,
                        },
                        "before": {
                            "type": "string", "maxLength": 40,
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "text_filter": {"type": "string", "minLength": 1, "maxLength": 1024},
                        "cursor": {
                            "type": "string", "maxLength": 64,
                        },
                        "limit": {
                            "type": "integer", "minimum": 1,
                            "maximum": INTERACTIVE_HISTORY_PAGE_LIMIT,
                            "default": HISTORY_PAGE_LIMIT,
                        },
                    },
                    "required": ["channel_id", "after"],
                    "additionalProperties": False,
                },
                usage_note=(
                    "text_filter matches full text (content, embeds, attachment names), "
                    "case-insensitively. For bot logs, continue cursors. Dates default to UTC."
                ),
            ),
            read_discord_channel_history,
            contract=CapabilityContract(
                entity_fields=(
                    ("channel_id", "discord_channel"), ("author_id", "discord_member"),
                ),
                source_scope="channel_messages", channel_fields=("channel_id",),
                result_channel_lists=(("messages", "channel_id"),),
                result_sources_within_query=True,
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_message_context",
                description=(
                    "Read messages around a visible Discord message."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "integer", "minimum": 1},
                        "message_id": {"type": "integer", "minimum": 1},
                        "after": {"type": "string", "maxLength": 40},
                        "before": {"type": "string", "maxLength": 40},
                        "limit": {
                            "type": "integer",
                            "minimum": 3,
                            "maximum": CONTEXT_MESSAGE_LIMIT,
                            "default": 15,
                        },
                    },
                    "required": ["channel_id", "message_id"],
                    "additionalProperties": False,
                },
            ),
            read_message_context,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("message_id", "discord_message"),
                ),
                source_scope="channel_messages", channel_fields=("channel_id",),
                result_channel_fields=("channel_id",), result_sources_within_query=True,
            ),
        ),
    )

