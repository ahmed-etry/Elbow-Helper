"""Primary roster administration workflows."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from .automation import RosterAutomationService
from ..repository import RosterRepository
from ..models import Roster
from ..models import RosterLayout
from ..models import RosterMember
from .posts import RosterPostService
from .roles import RosterRoleSynchronizer
from .scheduling import due_window
from .scheduling import ScheduleWindow
from .search import RosterSearchCache


class RosterCapacityError(ValueError):
    def __init__(self, current_count: int):
        self.current_count = current_count
        super().__init__(f"Roster has {current_count} current signups")


class RosterDeleteCleanupError(RuntimeError):
    def __init__(
        self,
        *,
        member_ids: tuple[int, ...] = (),
        message_ids: tuple[int, ...] = (),
    ) -> None:
        self.member_ids = member_ids
        self.message_ids = message_ids
        super().__init__("Roster Discord cleanup did not complete")


class RosterService:
    """Coordinate roster settings, timing, lifecycle, and stored state."""

    def __init__(
        self,
        repository: RosterRepository,
        search: RosterSearchCache,
        roles: RosterRoleSynchronizer,
        posts: RosterPostService,
        automation: RosterAutomationService,
    ):
        self._repository = repository
        self._search = search
        self._roles = roles
        self._posts = posts
        self._automation = automation

    async def get(self, roster_id: int) -> Roster | None:
        return await asyncio.to_thread(self._repository.get_roster, roster_id)

    async def list_for_guild(self, guild_id: int) -> list[Roster]:
        return await asyncio.to_thread(
            self._repository.list_rosters,
            guild_id,
        )

    async def list_members(self, roster: Roster) -> list[RosterMember]:
        return await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )

    async def get_layout(self, roster_id: int) -> RosterLayout:
        return await asyncio.to_thread(
            self._repository.get_layout,
            roster_id,
        )

    async def create(
        self,
        *,
        guild_id: int,
        name: str,
        clan_code: str,
        role_id: int | None,
        max_members: int,
    ) -> Roster:
        roster = await asyncio.to_thread(
            self._repository.create_roster,
            guild_id=guild_id,
            name=name,
            clan_code=clan_code,
            role_id=role_id,
            max_members=max_members,
        )
        self._search.upsert(roster)
        return roster

    async def update(
        self,
        roster: Roster,
        changes: dict[str, object],
    ) -> Roster:
        if "max_members" in changes:
            current_members = await asyncio.to_thread(
                self._repository.list_members,
                roster.id,
                roster.active_cycle_id,
            )
            requested = int(changes["max_members"])
            if requested < len(current_members):
                raise RosterCapacityError(len(current_members))

        previous_role_id = roster.role_id
        updated = await asyncio.to_thread(
            self._repository.update_roster,
            roster.id,
            **changes,
        )
        self._search.upsert(updated)
        if previous_role_id != updated.role_id and updated.active_cycle_id:
            members = await asyncio.to_thread(
                self._repository.list_members,
                updated.id,
                updated.active_cycle_id,
            )
            member_ids = {row.discord_user_id for row in members}
            if previous_role_id:
                previous = replace(updated, role_id=previous_role_id)
                for member_id in member_ids:
                    await self._roles.sync(
                        previous,
                        member_id,
                        should_have=False,
                    )
            if updated.role_id:
                for member_id in member_ids:
                    await self._roles.sync(
                        updated,
                        member_id,
                        should_have=True,
                    )
        await self._posts.refresh(updated)
        return updated

    async def update_layout(
        self,
        roster_id: int,
        **changes: object,
    ) -> tuple[Roster | None, RosterLayout | None]:
        roster = await self.get(roster_id)
        if roster is None:
            return None, None
        layout = await asyncio.to_thread(
            self._repository.update_layout,
            roster.id,
            **changes,
        )
        await self._posts.refresh(roster)
        return roster, layout

    async def clear_one_off_timing(self, roster: Roster) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.update_roster,
            roster.id,
            one_off_open_ts=None,
            one_off_close_ts=None,
            last_open_cycle_key=None,
            last_close_cycle_key=None,
        )
        await self._posts.refresh(updated)
        return updated

    async def set_one_off_timing(
        self,
        roster: Roster,
        window: ScheduleWindow,
        *,
        reset_on_open: bool,
        now: datetime,
    ) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.update_roster,
            roster.id,
            one_off_open_ts=int(window.opens_at.timestamp()),
            one_off_close_ts=int(window.closes_at.timestamp()),
            reset_on_open=reset_on_open,
            status="closed",
            last_open_cycle_key=None,
            last_close_cycle_key=None,
        )
        if window.opens_at <= now < window.closes_at:
            await self._automation.open_scheduled(updated, window.cycle_key)
            return await self.get(updated.id) or updated
        await self._posts.refresh(updated)
        return updated

    async def disable_schedule(
        self,
        roster: Roster,
        changes: dict[str, object],
    ) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.update_roster,
            roster.id,
            **changes,
        )
        await self._posts.refresh(updated)
        return updated

    async def configure_schedule(
        self,
        roster: Roster,
        *,
        timezone_name: str,
        open_day: str,
        open_time: str,
        close_day: str,
        close_time: str,
        reset_on_open: bool,
        now: datetime,
    ) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.configure_schedule,
            roster.id,
            enabled=True,
            timezone_name=timezone_name,
            open_day=open_day,
            open_time=open_time,
            close_day=close_day,
            close_time=close_time,
            reset_on_open=reset_on_open,
        )
        current_window = due_window(updated, now)
        if current_window is not None and now < current_window.closes_at:
            if updated.last_close_cycle_key == current_window.cycle_key:
                await self._posts.refresh(updated)
            elif (
                updated.last_open_cycle_key == current_window.cycle_key
                and updated.active_cycle_id
            ):
                updated = await asyncio.to_thread(
                    self._repository.update_roster,
                    updated.id,
                    status="open",
                )
                await self._posts.refresh(updated)
            else:
                await self._automation.open_scheduled(
                    updated,
                    current_window.cycle_key,
                )
        elif (
            current_window is not None
            and updated.last_close_cycle_key != current_window.cycle_key
        ):
            await self._automation.close_scheduled(
                updated,
                current_window.cycle_key,
            )
        else:
            await self._posts.refresh(updated)
        return await self.get(updated.id) or updated

    async def open(self, roster: Roster) -> Roster:
        updated = await self._automation.ensure_open_cycle(roster)
        await self._posts.refresh(updated)
        return updated

    async def close(self, roster: Roster) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.close_cycle,
            roster.id,
        )
        await self._posts.refresh(updated)
        return updated

    async def toggle_buttons(self, roster: Roster) -> Roster:
        updated = await asyncio.to_thread(
            self._repository.update_roster,
            roster.id,
            buttons_hidden=not roster.buttons_hidden,
        )
        await self._posts.refresh(updated)
        return updated

    async def clone(
        self,
        source: Roster,
        *,
        name: str,
        clan_code: str | None,
        role_id: int | None,
        max_members: int | None,
        min_townhall: int | None,
    ) -> Roster:
        clone = await asyncio.to_thread(
            self._repository.clone_roster,
            source.id,
            name=name,
            clan_code=clan_code,
            role_id=role_id,
            max_members=max_members,
            min_townhall=min_townhall,
        )
        self._search.upsert(clone)
        return clone

    async def delete(self, roster: Roster) -> None:
        members = await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )
        member_ids = tuple(sorted({row.discord_user_id for row in members}))
        for member_id in member_ids:
            cleaned = await self._roles.sync(
                roster,
                member_id,
                should_have=False,
            )
            if not cleaned:
                raise RosterDeleteCleanupError(member_ids=(member_id,))

        failed_message_ids = await self._posts.disable_all(roster)
        if failed_message_ids:
            raise RosterDeleteCleanupError(message_ids=failed_message_ids)

        await asyncio.to_thread(
            self._repository.delete_roster,
            roster.id,
        )
        self._search.remove(roster)
