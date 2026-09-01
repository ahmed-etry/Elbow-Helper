"""Read-only cross-feature contract for roster data."""

from __future__ import annotations

import asyncio

from ..repository import RosterRepository
from ..models import Roster
from ..models import RosterMember


class RosterQueries:
    """Expose supported roster reads without exposing repository internals."""

    def __init__(self, repository: RosterRepository):
        self._repository = repository

    async def get(self, roster_id: int) -> Roster | None:
        return await asyncio.to_thread(self._repository.get_roster, roster_id)

    async def list_for_guild(self, guild_id: int) -> list[Roster]:
        return await asyncio.to_thread(
            self._repository.list_rosters,
            guild_id,
        )

    async def members(self, roster: Roster) -> list[RosterMember]:
        return await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )

    async def members_for_user(
        self,
        roster_ids: tuple[int, ...],
        member_id: int,
    ) -> dict[int, list[RosterMember]]:
        return await asyncio.to_thread(
            self._repository.list_members_for_user,
            roster_ids,
            member_id,
        )

    async def role_has_signup(self, role_id: int, member_id: int) -> bool:
        return await asyncio.to_thread(
            self._repository.role_has_signup,
            role_id,
            member_id,
        )

    async def post_message_ids_for_channel(self, channel_id: int) -> set[int]:
        posts = await asyncio.to_thread(self._repository.list_posts)
        return {
            post.message_id
            for post in posts
            if post.channel_id == channel_id
        }
