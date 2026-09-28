"""Build command capabilities from registered slash commands and help."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import ParameterInfo, discover_commands
from elbow_helper.infrastructure.ai import AgentToolDefinition


@dataclass(frozen=True, slots=True)
class CommandAdapter:
    path: str
    delivery: str
    run: Callable[[Any, Mapping[str, Any]], Awaitable[Any]]
    options: tuple[ParameterInfo, ...] = ()
    entity_options: tuple[tuple[str, str], ...] = ()
    check_period: Callable[[Mapping[str, Any], Mapping[str, Any]], str] | None = None


@dataclass(frozen=True, slots=True)
class CommandCapability:
    definition: AgentToolDefinition
    adapter: CommandAdapter
    required: tuple[str, ...]
    option_info: tuple[ParameterInfo, ...]


_TYPES = {
    "string": "string", "integer": "integer", "number": "number",
    "boolean": "boolean", "user": "integer", "member": "integer",
    "channel": "integer", "role": "integer", "attachment": "integer",
}


def _option_schema(option: ParameterInfo) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _TYPES.get(option.type_name, "string")}
    if option.description:
        schema["description"] = option.description
    if option.choice_values:
        schema["enum"] = list(option.choice_values)
        schema["description"] = (
            (option.description + " " if option.description else "")
            + "Choices: " + ", ".join(
                f"{label} ({value})" for label, value in zip(option.choices, option.choice_values)
            )
        )
    return schema


def build_command_capabilities(
    bot: Any, adapters: Sequence[CommandAdapter],
) -> dict[str, CommandCapability]:
    """Expose only adapters backed by both a command and a help entry."""
    commands = discover_commands(bot)
    help_entries = {entry.path: entry for entry in HELP_ENTRIES}
    result: dict[str, CommandCapability] = {}
    for adapter in adapters:
        command = commands.get(adapter.path)
        help_entry = help_entries.get(adapter.path)
        if command is None or help_entry is None:
            continue
        options = (*command.parameters, *adapter.options)
        if len({option.name for option in options}) != len(options):
            raise ValueError("Command options must have distinct names")
        name = "run_command_" + adapter.path.lstrip("/").replace(" ", "_")
        if name in result:
            raise ValueError("Command capability already exists")
        definition = AgentToolDefinition(
            name=name,
            description=(f"{help_entry.summary} {help_entry.details} "
                         + ("The result is private." if adapter.delivery == "private" else "")).strip(),
            parameters={
                "type": "object",
                "properties": {option.name: _option_schema(option) for option in options},
                "required": [],
                "x-command-required": [option.name for option in options if option.required],
                "additionalProperties": False,
            },
        )
        result[name] = CommandCapability(
            definition=definition,
            adapter=adapter,
            required=tuple(option.name for option in options if option.required),
            option_info=options,
        )
    return result
