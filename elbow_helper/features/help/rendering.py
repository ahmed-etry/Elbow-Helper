from __future__ import annotations

import discord
from elbow_helper.discord.pagination import format_page_footer

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX

from .catalog import HelpEntry
from .discovery import DiscoveredCommand, ParameterInfo

EMBED_COLOR = discord.Color(DEFAULT_EMBED_COLOR_HEX)


def _format_parameter(parameter: ParameterInfo) -> str:
    line = f"`{parameter.name}`"
    if not parameter.required:
        line += " *(optional)*"
    details: list[str] = []
    if parameter.description:
        details.append(parameter.description)
    if parameter.choices:
        details.append(f"Choices: {', '.join(parameter.choices)}")
    if not details:
        return line
    return f"{line} — {' '.join(details)}"


def _build_parameter_block(parameters: tuple[ParameterInfo, ...]) -> str:
    return "\n".join(f"- {_format_parameter(parameter)}" for parameter in parameters)


def _build_description(entry: HelpEntry) -> str:
    return entry.details or entry.summary


def build_list_embed(entries: list[HelpEntry], page: int, page_size: int, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    start = page * page_size
    end = min(start + page_size, len(entries))
    for entry in entries[start:end]:
        embed.add_field(
            name=entry.path,
            value=entry.summary,
            inline=False,
        )
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    embed.set_footer(text=format_page_footer(page + 1, total_pages))
    return embed


def build_detail_embed(entry: HelpEntry, discovered: DiscoveredCommand | None) -> discord.Embed:
    embed = discord.Embed(
        title=entry.path,
        description=_build_description(entry),
        color=EMBED_COLOR,
    )

    parameters = discovered.parameters if discovered else ()
    if parameters:
        embed.add_field(
            name="Options",
            value=_build_parameter_block(parameters),
            inline=False,
        )

    if entry.examples:
        examples = "\n".join(entry.examples)
        embed.add_field(
            name="Example" if len(entry.examples) == 1 else "Examples",
            value=f"```\n{examples}\n```",
            inline=False,
        )
    if entry.notes:
        embed.add_field(
            name="Notes",
            value="\n".join(f"- {note}" for note in entry.notes),
            inline=False,
        )

    if discovered is None:
        embed.set_footer(text="Command options couldn't be loaded.")
    return embed
