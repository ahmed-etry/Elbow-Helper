"""Model-facing capability definitions for Discord reads and actions."""

from __future__ import annotations
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..engine.capability_contract import CapabilityContract
from ..actions.contracts import ActionClass
from ..models import AgentCapabilityEffect, RegisteredAgentTool
from .messages import prepare_post, prepare_edit, prepare_delete, find_agent_files, content_options


def discord_message_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="find_agent_files",
            description="List files the agent delivered earlier in this conversation. Returns their filename and reply message ID for posting a selected file.",
            parameters={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "required": [], "additionalProperties": False},
        ), find_agent_files,
            contract=CapabilityContract(
                entity_fields=(), source_scope="request_context",
                filter_fields=("offset", "limit"),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="post_discord_message",
            description="Post text in a channel visible and writable by both the asker and bot; long text is split. Can attach a file made in this conversation. Pings require explicit preview values. Returns each posted message ID.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
            **content_options(),
                "file_name": {"type": "string", "minLength": 1, "maxLength": 255},
                "file_message_id": {"type": "integer", "minimum": 1},
            }, "required": ["channel_id", "text"], "additionalProperties": False},
        ), prepare_post, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("ping_role_ids", "discord_role_set"),
                    ("file_message_id", "agent_file_message"),
                ),
                source_scope="request_context",
                filter_fields=("text", "ping_everyone", "file_name"),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="edit_agent_message",
            description="Edit text in a message posted by post_discord_message. Pings require explicit preview values.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
                "message_id": {"type": "integer", "minimum": 1},
            **content_options(),
            }, "required": ["channel_id", "message_id", "text"],
               "additionalProperties": False},
        ), prepare_edit, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("message_id", "discord_message"),
                    ("ping_role_ids", "discord_role_set"),
                ), source_scope="request_context", filter_fields=("text", "ping_everyone")),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="delete_agent_message",
            description="Delete one message posted by post_discord_message. This cannot be undone.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
                "message_id": {"type": "integer", "minimum": 1},
            }, "required": ["channel_id", "message_id"],
               "additionalProperties": False},
        ), prepare_delete, AgentCapabilityEffect.COMMAND, ActionClass.IRREVERSIBLE,
            contract=CapabilityContract(entity_fields=(
                    ("channel_id", "discord_channel"),
                    ("message_id", "discord_message"),
                ), source_scope="request_context"),
        ),
    )

