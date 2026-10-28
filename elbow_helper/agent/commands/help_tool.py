"""Read canonical command help permitted for the asker and destination."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import discover_commands
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import ACCESS_ROLE_SETS, has_lookup_access, has_access_requirements, require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool


def command_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="read_bot_command_help",
        description=(
            "Read command help and registered options permitted here for your roles."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 100},
                "command_path": {"type": "string", "minLength": 1, "maxLength": 100},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": [], "additionalProperties": False,
        },
        usage_note='command_path: exact slash-command options or group subcommands; overrides query. query searches summaries. A scope gap does not mean a command is absent.',
    ), read_bot_command_help,
        contract=CapabilityContract(entity_fields=()),
            ),)


async def read_bot_command_help(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    discovered = discover_commands(context.bot)
    query = str(arguments.get("query") or "").strip().casefold()
    command_path = str(arguments.get('command_path') or '').strip().casefold()
    # A typed path is the more precise selector when the planner supplies both.
    if command_path:
        query = ''
    exact = command_path or query
    exact = exact if exact.startswith("/") else f"/{exact}"
    entries = []
    scope_blocked = False
    entry_levels = {}
    requester_levels = {}
    for entry in HELP_ENTRIES:
        if entry.path not in discovered:
            continue
        if command_path and not (entry.path.casefold() == exact or entry.path.casefold().startswith(exact + ' ')):
            continue
        if (query and entry.path.casefold() != exact
                and not any(query in value.casefold()
                            for value in (entry.path, entry.summary, entry.details))):
            continue
        if entry.visible_to is not None:
            level = next((level for level, roles in ACCESS_ROLE_SETS.items()
                          if roles == entry.visible_to), None)
            if level is None or not has_access_requirements(context.guild, context.member.id, {level}):
                continue
            # Department rooms include Core observers. Command instructions can
            # be read there by a qualified operator without treating every room
            # observer as qualified to invoke the command.
            disclosure = level
            if not has_lookup_access(context, {level}):
                disclosure = next((candidate for candidate in ('recruitment', 'cwl', 'promotion')
                    if entry.visible_to <= ACCESS_ROLE_SETS[candidate]
                    and has_lookup_access(context, {candidate})), None)
            if disclosure is None:
                scope_blocked = True
                continue
            entry_levels[entry.path] = disclosure
            requester_levels[entry.path] = level
        entries.append(entry)
    exact_entry = next((entry for entry in entries if entry.path.casefold() == exact), None)
    if exact_entry is not None:
        command = discovered[exact_entry.path]
        if level := entry_levels.get(exact_entry.path):
            context.state.required_access.add(level)
            context.state.help_requester_access.add(requester_levels[exact_entry.path])
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
    matches = entries
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 10)
    page = matches[offset:offset + limit]
    context.state.required_access.update(
        entry_levels[entry.path] for entry in page if entry.path in entry_levels
    )
    context.state.help_requester_access.update(
        requester_levels[entry.path] for entry in page if entry.path in requester_levels)
    await require_evidence_access(context)
    return {
        "commands": [{
            "path": entry.path,
            "summary": entry.summary,
            "category": entry.category,
        } for entry in page],
        "matching_count": len(matches),
        "next_offset": offset + limit if offset + limit < len(matches) else None,
        "source": "/help",
        "visibility": "role_and_destination_permitted",
        **({'error': 'Matching command help is outside this destination scope.',
            'cause': 'scope_refused', 'available_privately': True, 'selection_complete': False}
           if scope_blocked and not entries else {}),
    }
