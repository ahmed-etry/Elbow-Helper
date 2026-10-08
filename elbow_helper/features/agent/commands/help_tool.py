"""Read the public portion of the canonical command help catalogue."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import discover_commands
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool


def command_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="read_bot_command_help",
        description=(
            "Find current public /help entries and registered command options by "
            "path or purpose. Restricted help entries are excluded because this "
            "conversation may be visible to others. This explains commands; it "
            "does not execute them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 100},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": [], "additionalProperties": False,
        },
    ), read_bot_command_help,
        contract=CapabilityContract(entity_fields=()),
            ),)


async def read_bot_command_help(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    discovered = discover_commands(context.bot)
    query = str(arguments.get("query") or "").strip().casefold()
    exact = query if query.startswith("/") else f"/{query}"
    entries = [
        entry for entry in HELP_ENTRIES
        if entry.visible_to is None and entry.path in discovered
    ]
    exact_entry = next((entry for entry in entries if entry.path.casefold() == exact), None)
    if exact_entry is not None:
        command = discovered[exact_entry.path]
        await require_evidence_access(context)
        return {
            "path": exact_entry.path,
            "summary": exact_entry.summary,
            "details": exact_entry.details,
            "category": exact_entry.category,
            "examples": list(exact_entry.examples),
            "notes": list(exact_entry.notes),
            "options": [asdict(item) for item in command.parameters],
            "source": "/help",
        }
    matches = [
        entry for entry in entries
        if not query or query in entry.path.casefold()
        or query in entry.summary.casefold()
        or query in entry.details.casefold()
    ]
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 10)
    await require_evidence_access(context)
    return {
        "commands": [{
            "path": entry.path,
            "summary": entry.summary,
            "category": entry.category,
        } for entry in matches[offset:offset + limit]],
        "matching_count": len(matches),
        "next_offset": offset + limit if offset + limit < len(matches) else None,
        "source": "/help",
        "visibility": "public_entries_only",
    }
