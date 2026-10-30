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
                            "default": 25,
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
                        "author_ids": {"type": "array", "minItems": 1, "maxItems": 200,
                                       "uniqueItems": True,
                                       "items": {"type": "integer", "minimum": 1}},
                        "mentions": {"type": "array", "minItems": 1, "maxItems": 20,
                                     "items": {"type": "integer", "minimum": 1}},
                        "after": {"type": "string", "maxLength": 40},
                        "before": {"type": "string", "maxLength": 40},
                    },
                    "additionalProperties": False,
                },
                usage_note=(
                    "Use words, authors, mentions, channels or dates; any filter suffices. "
                    "Omit channels for server-wide search. Follow next_cursor for more."
                ),
            ),
            search_discord_messages,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("channel_ids", "discord_channel_set"),
                    ("author_id", "discord_member"),
                    ("author_ids", "discord_member_set"),
                    ("mentions", "discord_member_set"),
                ),
                source_scope="channel_messages",
                # Multi-channel searches drop unavailable channels inside the reader.
                channel_fields=("channel_id",),
                result_channel_lists=(("matches", "channel_id"),),
                result_sources_within_query=True,
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_channel_history",
                description=(
                    "Read channel history chunks for semantic review; cursors retain the window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "fresh": {"type": "boolean", "description": "Fetch this page again, bypassing retained pages."},
                        "channel_id": {"type": "integer", "minimum": 1},
                        "after": {
                            "type": "string", "maxLength": 40,
                        },
                        "before": {
                            "type": "string", "maxLength": 40,
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "pages": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1},
                        "cursor": {
                            "type": "string", "maxLength": 64,
                        },
                        "limit": {
                            "type": "integer", "minimum": 1,
                            "maximum": INTERACTIVE_HISTORY_PAGE_LIMIT,
                            "default": HISTORY_PAGE_LIMIT,
                        },
                    },
                    "required": ["channel_id"],
                    "additionalProperties": False,
                },
                usage_note=(
                    "Select relevance by meaning. Omit after for unknown start. pages batches scans. "
                    "Continue channel_id+cursor for older context within budget."
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
                    "Read a Discord message's context and its explicit reply target."
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

