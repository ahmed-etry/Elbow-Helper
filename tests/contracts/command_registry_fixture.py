"""Minimal command registry populated with the cogs used by signup help."""

import discord
from discord import app_commands

from elbow_helper.features.clan_transfers.cog import ClanTransfers
from elbow_helper.features.cwl.cog import CwlManagement
from elbow_helper.features.rosters.cog import Rosters


class CommandRegistryBot:
    def __init__(self) -> None:
        client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(client)
        self._cogs = {
            "CwlManagement": object.__new__(CwlManagement),
            "Rosters": object.__new__(Rosters),
            "ClanTransfers": object.__new__(ClanTransfers),
        }

    def get_cog(self, name: str):
        return self._cogs.get(name)
