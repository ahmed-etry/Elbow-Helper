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
                description="Find accessible Discord channels by name or ID. Use the result to identify the channel requested by the asker.",
                parameters={
                    "type": "object", "properties": {"query": {"type": "string", "maxLength": 100}},
                    "required": ["query"], "additionalProperties": False,
                },
            ),
            find_discord_channels,
            contract=CapabilityContract(
                result_paths=(('channels', 'N', 'channel_id'),),
                result_path_kinds=((('channels', 'N', 'channel_id'), 'discord_channel'),),
                entity_fields=(),
                time_fields=(),
                source_scope="channel_locator",
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="search_discord_messages",
                description=(
                    "Search accessible Discord history by author, words, channels or dates; omit query for author history. "
                    "Honour the scope requested by the asker. "
                    "For one explicit channel, continue with the returned cursor when broader coverage "
                    "is needed. Results are not proof of absence; read surrounding messages when needed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Narrow words or phrase to search for.",
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
                            "description": "Explicit accessible channels to search together.",
                        },
                        "cursor": {
                            "type": "string", "maxLength": 64,
                            "description": "Continuation cursor returned by the same channel search and filters.",
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "after": {"type": "string", "maxLength": 40, "description": "Inclusive ISO 8601 date or timestamp. Use UTC when no offset is supplied."},
                        "before": {"type": "string", "maxLength": 40, "description": "Exclusive ISO 8601 date or timestamp. Use UTC when no offset is supplied."},
                    },
                    "additionalProperties": False,
                },
            ),
            search_discord_messages,
            contract=CapabilityContract(
                result_path_kinds=(
                    (('matches', 'N', 'channel_id'), 'discord_channel'),
                    (('matches', 'N', 'message_id'), 'discord_message'),
                    (('matches', 'N', 'author_id'), 'discord_member'),
                ),
                result_paths=(
                    ('matches', 'N', 'channel_id'),
                    ('matches', 'N', 'message_id'),
                    ('matches', 'N', 'author_id'),
                    ('coverage', 'next_cursor'),
                ),
                entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("channel_ids", "discord_channel_set"),
                    ("author_id", "discord_member"),
                ),
                time_fields=("after", "before", "cursor"),
                source_scope="channel_messages",
                channel_fields=("channel_id", "channel_ids"),
                result_channel_lists=(("matches", "channel_id"),),
                result_sources_within_query=True,
                time_window=("after", "before", "iso_utc"),
                optional_time_window=True,
                bounded_fields=("cursor",),
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_channel_history",
                description=(
                    "Read one bounded page of every currently available message "
                    "in one accessible Discord channel or thread from an inclusive "
                    "start through an exclusive end (or the current request). This "
                    "does not depend on keyword search indexing; continue with the "
                    "returned cursor for broader coverage. Up to 100 messages fit "
                    "in a call; long pages stop earlier with a continuation cursor. "
                    "Use this for a period review, then search for specific gaps."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "channel_id": {"type": "integer", "minimum": 1},
                        "after": {
                            "type": "string", "maxLength": 40,
                            "description": (
                                "Inclusive ISO 8601 start date or timestamp. "
                                "Use UTC when no offset is supplied."
                            ),
                        },
                        "before": {
                            "type": "string", "maxLength": 40,
                            "description": (
                                "Exclusive ISO 8601 end date or timestamp. "
                                "Omit to stop at the current request."
                            ),
                        },
                        "author_id": {"type": "integer", "minimum": 1},
                        "cursor": {
                            "type": "string", "maxLength": 64,
                            "description": (
                                "Continuation cursor returned for this same "
                                "channel, period and author filter."
                            ),
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
            ),
            read_discord_channel_history,
            contract=CapabilityContract(
                result_path_kinds=(
                    (('messages', 'N', 'channel_id'), 'discord_channel'),
                    (('messages', 'N', 'message_id'), 'discord_message'),
                    (('messages', 'N', 'author_id'), 'discord_member'),
                ),
                result_paths=(
                    ('messages', 'N', 'channel_id'),
                    ('messages', 'N', 'message_id'),
                    ('messages', 'N', 'author_id'),
                    ('coverage', 'next_cursor'),
                ),
                entity_fields=(("channel_id", "discord_channel"), ("author_id", "discord_member")),
                time_fields=("after", "before", "cursor"),
                source_scope="channel_messages",
                channel_fields=("channel_id",),
                result_channel_lists=(("messages", "channel_id"),),
                result_sources_within_query=True,
                time_window=("after", "before", "iso_utc"),
                bounded_fields=("cursor",),
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_message_context",
                description=(
                    "Read messages surrounding an accessible Discord message identified by a search "
                    "result or message link, so its meaning can be assessed in context."
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
            contract=CapabilityContract(
                result_paths=(('channel_id',), ('messages', 'N', 'message_id'), ('messages', 'N', 'author_id')),
                result_path_kinds=(
                    (('channel_id',), 'discord_channel'),
                    (('messages', 'N', 'message_id'), 'discord_message'),
                    (('messages', 'N', 'author_id'), 'discord_member'),
                ),
                entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("message_id", "discord_message"),
                ),
                time_fields=("after", "before"),
                source_scope="channel_messages",
                channel_fields=("channel_id",),
                result_channel_fields=("channel_id",),
                result_sources_within_query=True,
                time_window=("after", "before", "iso_utc"),
            ),
        ),
    )

