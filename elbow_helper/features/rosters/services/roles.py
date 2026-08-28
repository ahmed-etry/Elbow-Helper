"""Discord role synchronization for roster membership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

import discord
from discord.ext import commands

from ..repository import RosterRepository
from ..models import Roster


LOGGER = logging.getLogger(__name__)

WarRoleClaim = Callable[[int, int], bool]


class RosterRoleSynchronizer:
    """Reconcile a roster role without owning signup workflow decisions."""

    def __init__(
        self,
        bot: commands.Bot,
        repository: RosterRepository,
        war_role_claim: WarRoleClaim,
    ):
        self._bot = bot
        self._repository = repository
        self._war_role_claim = war_role_claim

    async def sync(
        self,
        roster: Roster,
        member_id: int,
        *,
        should_have: bool,
    ) -> bool:
        if roster.role_id is None:
            return True
        guild = self._bot.get_guild(roster.guild_id)
        if guild is None:
            return False
        member = guild.get_member(member_id)
        role = guild.get_role(roster.role_id)
        if member is None or role is None:
            return False
        try:
            if should_have and role not in member.roles:
                await member.add_roles(role, reason=f"Signed up for roster: {roster.name}")
            elif not should_have and role in member.roles:
                needed = await asyncio.to_thread(
                    self._repository.role_still_needed,
                    roster.role_id,
                    member_id,
                    excluding_roster_id=roster.id,
                )
                if not needed:
                    needed = self._war_role_claim(roster.role_id, member_id)
                if not needed:
                    await member.remove_roles(role, reason=f"Left roster: {roster.name}")
            return True
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Could not sync roster role guild=%s member=%s role=%s",
                roster.guild_id,
                member_id,
                roster.role_id,
            )
            return False
