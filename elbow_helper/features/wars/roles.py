"""Regular-war role reconciliation against live in-game lineups."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
import logging
import sqlite3
from typing import Any

import discord

from elbow_helper.domain.player_tags import canonical_player_tag
from elbow_helper.configuration.clans import CLAN_CODES_BY_NAME, CLAN_WAR_ROLE_IDS
from elbow_helper.configuration.guild import GUILD_ID

LOGGER = logging.getLogger(__name__)

_ACTIVE_REGULAR_WAR_STATES = {"preparation", "inwar"}
_INACTIVE_WAR_STATES = {"notinwar", "warended"}
_CLAN_CODE_BY_WAR_ROLE_ID = {
    role_id: clan_code for clan_code, role_id in CLAN_WAR_ROLE_IDS.items()
}
RosterRoleClaim = Callable[[int, int], Awaitable[bool]]


class WarRoleMixin:
    """Keep regular-war roles aligned with roster and live-lineup claims."""

    def war_lineup_needs_role(self, role_id: int, member_id: int) -> bool:
        """Return whether a current in-game lineup claims this role."""
        clan_code = _CLAN_CODE_BY_WAR_ROLE_ID.get(role_id)
        if clan_code is None:
            return False
        return member_id in self.war_role_lineups.get(clan_code, {}).values()

    async def _linked_lineup(
        self,
        clan_code: str,
        members: list[dict[str, Any]],
    ) -> dict[str, int] | None:
        if self.account_links is None:
            LOGGER.warning(
                "War role sync skipped for %s: player links are unavailable",
                clan_code,
            )
            return None

        try:
            links = await asyncio.to_thread(self.account_links.get_all_links)
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("War role sync skipped for %s: %s", clan_code, error)
            return None
        if not isinstance(links, dict):
            LOGGER.warning("War role sync skipped for %s: player links are invalid", clan_code)
            return None

        normalized_links: dict[str, dict[str, Any]] = {}
        for raw_tag, row in links.items():
            tag = canonical_player_tag(raw_tag)
            if tag and isinstance(row, dict):
                normalized_links[tag] = row
        previous = self.war_role_lineups.get(clan_code, {})
        lineup: dict[str, int] = {}
        missing_tags: set[str] = set()
        for player in members:
            if not isinstance(player, dict):
                continue
            tag = canonical_player_tag(player.get("tag"))
            if not tag:
                continue
            row = normalized_links.get(tag)
            linked_id = row.get("discord_user_id") if row else None
            if isinstance(linked_id, int) and linked_id > 0:
                lineup[tag] = linked_id
            elif tag in previous:
                # Keep a claim stable if its established link disappears mid-war.
                lineup[tag] = previous[tag]
            else:
                missing_tags.add(tag)

        if missing_tags != self._war_role_missing_links.get(clan_code, set()):
            if missing_tags:
                LOGGER.info(
                    "War role sync for %s has %s unlinked lineup account(s)",
                    clan_code,
                    len(missing_tags),
                )
            self._war_role_missing_links[clan_code] = missing_tags
        return lineup

    async def _roster_needs_war_role(self, role_id: int, member_id: int) -> bool | None:
        checker = getattr(self, "_roster_role_claim", None)
        if checker is None:
            return None
        try:
            return bool(await checker(role_id, member_id))
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning(
                "War role cleanup deferred for member=%s role=%s: %s",
                member_id,
                role_id,
                error,
            )
            return None

    def set_roster_role_claim(self, checker: RosterRoleClaim) -> None:
        """Inject the supported roster role-claim contract."""
        self._roster_role_claim = checker

    async def _sync_war_roles(self, clan_name: str, data: dict[str, Any]) -> None:
        clan_code = CLAN_CODES_BY_NAME.get(clan_name)
        role_id = CLAN_WAR_ROLE_IDS.get(clan_code or "")
        if clan_code is None or role_id is None:
            return

        state = str(data.get("state") or "").strip().lower()
        is_cwl = bool(data.get("warTag"))
        if not is_cwl and state in _ACTIVE_REGULAR_WAR_STATES:
            clan = data.get("clan")
            members = clan.get("members") if isinstance(clan, dict) else None
            if not isinstance(members, list) or not members:
                LOGGER.warning(
                    "War role sync skipped for %s: active lineup is missing",
                    clan_code,
                )
                return
            desired_lineup = await self._linked_lineup(clan_code, members)
            if desired_lineup is None:
                return
        elif is_cwl or state in _INACTIVE_WAR_STATES:
            desired_lineup = {}
            self._war_role_missing_links.pop(clan_code, None)
        else:
            return

        lock = self._war_role_locks.setdefault(clan_code, asyncio.Lock())
        async with lock:
            previous_lineup = self.war_role_lineups.get(clan_code, {})
            previous_managed = self.war_role_managed_members.get(clan_code, set())
            desired_ids = set(desired_lineup.values())
            managed = set(previous_managed) | desired_ids

            guild = self.bot.get_guild(GUILD_ID)
            role = guild.get_role(role_id) if guild is not None else None
            if guild is None or role is None:
                LOGGER.warning(
                    "War role sync skipped for %s: guild or role is unavailable",
                    clan_code,
                )
                return

            # Publish the new claim before touching Discord roles so roster opt-outs
            # cannot remove a role that the current lineup still needs.
            self.war_role_lineups[clan_code] = desired_lineup

            for member_id in desired_ids:
                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                if role in member.roles:
                    continue
                try:
                    await member.add_roles(
                        role,
                        reason=f"Current regular-war lineup: {clan_code}",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.warning(
                        "Could not add war role clan=%s member=%s role=%s",
                        clan_code,
                        member_id,
                        role_id,
                    )

            retained_managed = set(managed)
            for member_id in managed - desired_ids:
                roster_needs_role = await self._roster_needs_war_role(role_id, member_id)
                if roster_needs_role is None:
                    continue
                if roster_needs_role:
                    retained_managed.discard(member_id)
                    continue

                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        retained_managed.discard(member_id)
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        continue
                if role not in member.roles:
                    retained_managed.discard(member_id)
                    continue
                try:
                    await member.remove_roles(
                        role,
                        reason=f"No longer in {clan_code} regular-war lineup",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.warning(
                        "Could not remove war role clan=%s member=%s role=%s",
                        clan_code,
                        member_id,
                        role_id,
                    )
                else:
                    retained_managed.discard(member_id)

            if retained_managed:
                self.war_role_managed_members[clan_code] = retained_managed
            else:
                self.war_role_managed_members.pop(clan_code, None)
            if not desired_lineup:
                self.war_role_lineups.pop(clan_code, None)

            if (
                desired_lineup != previous_lineup
                or retained_managed != previous_managed
            ):
                self._store_war_role_state()
                await self._save_cache_async()
