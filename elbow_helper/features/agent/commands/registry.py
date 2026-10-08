"""Build command capabilities from registered slash commands and help."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import ParameterInfo, discover_commands
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..actions.contracts import ActionClass, ChangePreview


@dataclass(frozen=True, slots=True)
class CommandAdapter:
    path: str
    delivery: str
    run: Callable[[Any, Mapping[str, Any]], Awaitable[Any]]
    options: tuple[ParameterInfo, ...] = ()
    entity_options: tuple[tuple[str, str], ...] = ()
    prepare: Callable[[Any, Mapping[str, Any]], Awaitable[Any]] | None = None
    action_class: ActionClass | None = None
    option_types: tuple[tuple[str, str], ...] = ()
    agent_details: str = ""
    capability_name: str | None = None

    def __post_init__(self) -> None:
        if self.delivery == "confirm" and self.classification not in (
            ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
        ):
            raise ValueError("Confirmed commands must be changes")
        if self.delivery == "confirm" and self.prepare is None:
            raise ValueError("Confirmed commands need a preview")
        if self.delivery != "confirm" and self.classification in (
            ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
        ):
            raise ValueError("Changes need a confirmation preview")

    @property
    def classification(self) -> ActionClass:
        if self.action_class is not None:
            return self.action_class
        return ActionClass.CHANGE if self.delivery == "confirm" else ActionClass.OUTPUT


@dataclass(frozen=True, slots=True)
class PreparedCommandChange:
    preview: ChangePreview
    run: Callable[[], Awaitable[Any]]

    def __post_init__(self) -> None:
        if not callable(self.run):
            raise ValueError("Prepared command needs a run handler")


@dataclass(frozen=True, slots=True)
class CommandCapability:
    definition: AgentToolDefinition
    adapter: CommandAdapter
    required: tuple[str, ...]
    option_info: tuple[ParameterInfo, ...]
    visible_to: frozenset[int] | None = None


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


def command_capability_name(adapter):
    return adapter.capability_name or adapter.path.lstrip("/").replace(" ", "_").replace("-", "_")


def legacy_capability_names():
    from ..capabilities import enabled_adapters
    return {
        "run_command_" + adapter.path.lstrip("/").replace(" ", "_"):
        command_capability_name(adapter)
        for adapter in enabled_adapters()
    }


def validate_command_names(adapters: Sequence[CommandAdapter]) -> None:
    from ..engine.registry import build_agent_tools
    names = set(build_agent_tools()) | {"find_gif"}
    for adapter in adapters:
        name = command_capability_name(adapter)
        if name in names:
            raise ValueError(f"Command capability already exists: {name}")
        names.add(name)


def build_command_capabilities(
    bot: Any, adapters: Sequence[CommandAdapter],
) -> dict[str, CommandCapability]:
    """Expose only adapters backed by both a command and a help entry."""
    validate_command_names(adapters)
    from ..engine.registry import build_agent_tools
    reserved_names = set(build_agent_tools())
    commands = discover_commands(bot)
    help_entries = {entry.path: entry for entry in HELP_ENTRIES}
    result: dict[str, CommandCapability] = {}
    for adapter in adapters:
        command = commands.get(adapter.path)
        help_entry = help_entries.get(adapter.path)
        if command is None or help_entry is None:
            continue
        type_overrides = dict(adapter.option_types)
        option_names = {option.name for option in (*command.parameters, *adapter.options)}
        if type_overrides.keys() - option_names:
            raise ValueError("Command type override names an unknown option")
        options = tuple(
            replace(option, type_name=type_overrides.get(option.name, option.type_name))
            for option in (*command.parameters, *adapter.options)
        )
        if len({option.name for option in options}) != len(options):
            raise ValueError("Command options must have distinct names")
        name = command_capability_name(adapter)
        if name in result or name in reserved_names:
            raise ValueError(f"Command capability already exists: {name}")
        definition = AgentToolDefinition(
            name=name,
            description=(f"{help_entry.summary} {adapter.agent_details} "
                         + ("Private result." if adapter.delivery == "private" else "")).strip(),
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
            visible_to=getattr(help_entry, "visible_to", None),
        )
    return result
