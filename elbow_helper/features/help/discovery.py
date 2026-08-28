from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import discord
from discord import app_commands

from elbow_helper.configuration.guild import GUILD_ID


@dataclass(frozen=True)
class ParameterInfo:
    name: str
    description: str
    required: bool
    type_name: str
    choices: tuple[str, ...] = ()
    autocomplete: bool = False


@dataclass(frozen=True)
class DiscoveredCommand:
    path: str
    description: str
    parameters: tuple[ParameterInfo, ...]


def _format_type_name(parameter: object) -> str:
    option_type = getattr(parameter, "type", None)
    if option_type is None:
        return "value"
    if hasattr(option_type, "name"):
        return str(option_type.name).replace("_", " ").lower()
    return str(option_type)


def _extract_choices(parameter: object) -> tuple[str, ...]:
    raw_choices = getattr(parameter, "choices", None) or ()
    names: list[str] = []
    for choice in raw_choices:
        label = getattr(choice, "name", None)
        if label is not None:
            names.append(str(label))
    return tuple(names)


def _extract_parameters(command: app_commands.Command) -> tuple[ParameterInfo, ...]:
    parameters = getattr(command, "parameters", None)
    if parameters is None:
        raw_params = getattr(command, "_params", {})
        parameters = tuple(raw_params.values())

    extracted: list[ParameterInfo] = []
    for parameter in parameters:
        name = getattr(parameter, "display_name", None) or getattr(parameter, "name", "value")
        description = getattr(parameter, "description", "") or ""
        if description == "...":
            description = ""
        extracted.append(
            ParameterInfo(
                name=str(name),
                description=str(description),
                required=bool(getattr(parameter, "required", False)),
                type_name=_format_type_name(parameter),
                choices=_extract_choices(parameter),
                autocomplete=bool(getattr(parameter, "autocomplete", False)),
            )
        )
    return tuple(extracted)


def _walk_commands(commands: Iterable[app_commands.Command | app_commands.Group], discovered: dict[str, DiscoveredCommand]) -> None:
    for command in commands:
        if isinstance(command, app_commands.Group):
            _walk_commands(command.commands, discovered)
            continue

        path = f"/{command.qualified_name}"
        discovered[path] = DiscoveredCommand(
            path=path,
            description=command.description or "",
            parameters=_extract_parameters(command),
        )


def discover_commands(bot: discord.Client) -> dict[str, DiscoveredCommand]:
    discovered: dict[str, DiscoveredCommand] = {}
    guild_commands = bot.tree.get_commands(guild=discord.Object(id=GUILD_ID))
    global_commands = bot.tree.get_commands()
    _walk_commands(guild_commands, discovered)
    _walk_commands(global_commands, discovered)
    return discovered
