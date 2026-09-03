"""Single composition root for shared guild slash-command groups."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID

ROOT_NAMES = ("cwl", "roster", "transfer", "record")


def _require_cog(bot: commands.Bot, name: str):
    cog = bot.get_cog(name)
    if cog is None:
        raise RuntimeError(f"Command registry requires {name}")
    return cog


def _command(
    cog,
    *,
    name: str,
    description: str,
    method: str,
) -> app_commands.Command:
    callback = getattr(type(cog), method)
    command = app_commands.Command(
        name=name,
        description=description,
        callback=callback,
    )
    command.binding = cog
    return command


def _group(
    name: str,
    description: str,
    definitions: tuple[tuple[object, str, str, str], ...],
) -> app_commands.Group:
    group = app_commands.Group(name=name, description=description)
    for cog, command_name, command_description, method in definitions:
        group.add_command(
            _command(
                cog,
                name=command_name,
                description=command_description,
                method=method,
            )
        )
    return group


async def setup(bot: commands.Bot) -> None:
    cwl = _require_cog(bot, "CwlManagement")
    rosters = _require_cog(bot, "Rosters")
    transfers = _require_cog(bot, "ClanTransfers")
    records = _require_cog(bot, "Records")
    guild = discord.Object(id=GUILD_ID)

    for name in (*ROOT_NAMES, "report"):
        if bot.tree.get_command(name, guild=guild) is not None:
            bot.tree.remove_command(name, guild=guild)

    groups = (
        _group(
            "cwl",
            "Manage CWL setup, updates, and reports.",
            (
                (
                    cwl,
                    "register",
                    "Connect a clan's CWL thread to its status updates.",
                    "register_cwl_thread",
                ),
                (
                    cwl,
                    "brief",
                    "Post the clan's CWL rules, rotations, and leadership details.",
                    "cwl_brief",
                ),
                (
                    cwl,
                    "bonus",
                    "Build a spreadsheet to help assign CWL bonus medals.",
                    "cwl_bonus",
                ),
                (
                    cwl,
                    "roster",
                    "Build a workbook to help assign CWL signups to clans.",
                    "cwl_roster",
                ),
            ),
        ),
        _group(
            "roster",
            "Create and run Discord signup rosters.",
            (
                (
                    rosters,
                    "create",
                    "Set up a roster members can join with linked Clash accounts.",
                    "roster_create",
                ),
                (
                    rosters,
                    "edit",
                    "Update a roster's name, clan, signup role, Town Hall minimum, or account limit.",
                    "roster_edit",
                ),
                (
                    rosters,
                    "timing",
                    "Set one opening and closing window for a roster.",
                    "roster_timing",
                ),
                (
                    rosters,
                    "schedule",
                    "Set automatic monthly opening and closing times for a roster.",
                    "roster_schedule",
                ),
                (
                    rosters,
                    "post",
                    "Post a roster in this channel.",
                    "roster_post",
                ),
                (
                    rosters,
                    "export",
                    "Export current roster signups to Google Sheets.",
                    "roster_export",
                ),
                (
                    rosters,
                    "list",
                    "See current rosters and their timing.",
                    "roster_list",
                ),
                (
                    rosters,
                    "clone",
                    "Create a roster using another roster's settings.",
                    "roster_clone",
                ),
                (
                    rosters,
                    "delete",
                    "Permanently remove a roster and its signup history.",
                    "roster_delete",
                ),
                (
                    cwl,
                    "announcement",
                    "Post CWL roster and transfer deadlines.",
                    "roster_announcement",
                ),
            ),
        ),
        _group(
            "transfer",
            "Request or manage family-clan transfers and CWL reminders.",
            (
                (
                    transfers,
                    "request",
                    "Ask to move to another family clan.",
                    "transfer_request",
                ),
                (
                    transfers,
                    "cancel",
                    "Cancel your pending move to a family clan.",
                    "transfer_cancel",
                ),
                (
                    cwl,
                    "reminder",
                    "Notify members who still need to move to their CWL clan.",
                    "transfer_reminder",
                ),
            ),
        ),
        _group(
            "record",
            "Add, edit, remove, or export leadership records.",
            (
                (
                    records,
                    "add",
                    "Document an incident involving a member.",
                    "record_add",
                ),
                (
                    records,
                    "export",
                    "Download all leadership records or one member's records.",
                    "record_export",
                ),
                (
                    records,
                    "edit",
                    "Update the category, type, or details of a member's record.",
                    "record_edit",
                ),
                (
                    records,
                    "remove",
                    "Delete one of a member's incident records.",
                    "record_remove",
                ),
            ),
        ),
    )
    for group in groups:
        bot.tree.add_command(group, guild=guild)


async def teardown(bot: commands.Bot) -> None:
    guild = discord.Object(id=GUILD_ID)
    for name in ROOT_NAMES:
        bot.tree.remove_command(name, guild=guild)
