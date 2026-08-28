"""Roster cycle transitions and scheduled automation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import datetime
from datetime import timezone as dt_timezone
import logging
import sqlite3

from discord.ext import commands

from ..config import CWL_SIGNUP_ROSTER_ID
from ..repository import RosterRepository
from ..models import Roster
from ..models import RosterMember
from .roles import RosterRoleSynchronizer
from .scheduling import due_window
from .scheduling import next_window
from .scheduling import ScheduleWindow


LOGGER = logging.getLogger(__name__)

RosterLock = Callable[[int], asyncio.Lock]
PostRefresher = Callable[[Roster], Awaitable[None]]


class RosterAutomationService:
    """Coordinate manual, one-off, and repeating roster cycles."""

    def __init__(
        self,
        bot: commands.Bot,
        repository: RosterRepository,
        roles: RosterRoleSynchronizer,
        lock_for: RosterLock,
        refresh_posts: PostRefresher,
    ):
        self._bot = bot
        self._repository = repository
        self._roles = roles
        self._lock_for = lock_for
        self._refresh_posts = refresh_posts

    async def cwl_signup_window(
        self,
        now: datetime,
    ) -> tuple[Roster, ScheduleWindow] | None:
        roster = await asyncio.to_thread(
            self._repository.get_roster,
            CWL_SIGNUP_ROSTER_ID,
        )
        if roster is None:
            LOGGER.error(
                "CWL signup roster is missing roster_id=%s",
                CWL_SIGNUP_ROSTER_ID,
            )
            return None
        if (
            roster.one_off_open_ts is not None
            and roster.one_off_close_ts is not None
        ):
            return (
                roster,
                ScheduleWindow(
                    cycle_key=f"once:{roster.one_off_open_ts}",
                    opens_at=datetime.fromtimestamp(
                        roster.one_off_open_ts,
                        dt_timezone.utc,
                    ),
                    closes_at=datetime.fromtimestamp(
                        roster.one_off_close_ts,
                        dt_timezone.utc,
                    ),
                ),
            )
        current = due_window(roster, now)
        if current is not None and now < current.closes_at:
            return roster, current
        upcoming = next_window(roster, now)
        return (roster, upcoming) if upcoming is not None else None

    async def claim_event(
        self,
        roster_id: int,
        cycle_key: str,
        event_key: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._repository.claim_automation_event,
            roster_id,
            cycle_key,
            event_key,
        )

    async def release_event(
        self,
        roster_id: int,
        cycle_key: str,
        event_key: str,
    ) -> None:
        await asyncio.to_thread(
            self._repository.release_automation_event,
            roster_id,
            cycle_key,
            event_key,
        )

    async def ensure_open_cycle(
        self,
        roster: Roster,
        *,
        now: datetime | None = None,
    ) -> Roster:
        now = now or datetime.now(dt_timezone.utc)
        if (
            roster.one_off_open_ts is not None
            and roster.one_off_close_ts is not None
        ):
            opens_at = datetime.fromtimestamp(
                roster.one_off_open_ts,
                dt_timezone.utc,
            )
            closes_at = datetime.fromtimestamp(
                roster.one_off_close_ts,
                dt_timezone.utc,
            )
            if not opens_at <= now < closes_at:
                return await self._set_status(roster.id, "closed")
            cycle_key = f"once:{roster.one_off_open_ts}"
            if roster.last_close_cycle_key == cycle_key:
                return await self._set_status(roster.id, "closed")
            if roster.last_open_cycle_key == cycle_key and roster.active_cycle_id:
                return await self._set_status(roster.id, "open")
            return await self._start_cycle(roster.id, cycle_key)

        if roster.schedule_enabled:
            window = due_window(roster, now)
            if window is None or not window.opens_at <= now < window.closes_at:
                return await self._set_status(roster.id, "closed")
            if roster.last_close_cycle_key == window.cycle_key:
                return await self._set_status(roster.id, "closed")
            if (
                roster.last_open_cycle_key == window.cycle_key
                and roster.active_cycle_id
            ):
                return await self._set_status(roster.id, "open")
            return await self._start_cycle(roster.id, window.cycle_key)

        return await asyncio.to_thread(
            self._repository.ensure_manual_cycle,
            roster.id,
        )

    async def _set_status(self, roster_id: int, status: str) -> Roster:
        return await asyncio.to_thread(
            self._repository.update_roster,
            roster_id,
            status=status,
        )

    async def _start_cycle(self, roster_id: int, cycle_key: str) -> Roster:
        roster = (
            await asyncio.to_thread(
                self._repository.start_cycle,
                roster_id,
                cycle_key,
            )
        )[0]
        self._bot.dispatch("roster_cycle_opened", roster)
        return roster

    async def open_scheduled(self, roster: Roster, cycle_key: str) -> None:
        async with self._lock_for(roster.id):
            current = await asyncio.to_thread(
                self._repository.get_roster,
                roster.id,
            )
            if current is None or current.last_open_cycle_key == cycle_key:
                return
            previous_members = await asyncio.to_thread(
                self._repository.list_members,
                current.id,
                current.active_cycle_id,
            )
            current = await self._start_cycle(current.id, cycle_key)
            if current.reset_on_open:
                for member_id in {
                    row.discord_user_id for row in previous_members
                }:
                    await self._roles.sync(
                        current,
                        member_id,
                        should_have=False,
                    )
            else:
                await self._carry_members_forward(current, previous_members)
            await self._refresh_posts(current)

    async def _carry_members_forward(
        self,
        roster: Roster,
        previous_members: list[RosterMember],
    ) -> None:
        by_member: dict[int, list[dict[str, object]]] = {}
        for row in previous_members:
            by_member.setdefault(row.discord_user_id, []).append(
                {
                    "player_tag": row.player_tag,
                    "player_name": row.player_name,
                    "clan_code": row.clan_code,
                    "townhall": row.townhall,
                    "hero_sum": row.hero_sum,
                }
            )
        for member_id, accounts in by_member.items():
            member_added, _ = await asyncio.to_thread(
                self._repository.add_members,
                roster.id,
                roster.active_cycle_id,
                member_id,
                accounts,
                roster.max_members,
                None,
            )
            await self._roles.sync(
                roster,
                member_id,
                should_have=member_added > 0,
            )

    async def close_scheduled(self, roster: Roster, cycle_key: str) -> None:
        async with self._lock_for(roster.id):
            current = await asyncio.to_thread(
                self._repository.get_roster,
                roster.id,
            )
            if current is None or current.last_close_cycle_key == cycle_key:
                return
            current = await asyncio.to_thread(
                self._repository.close_cycle,
                current.id,
                cycle_key,
            )
            await self._refresh_posts(current)

    async def run_due(self, now: datetime) -> None:
        for roster in await asyncio.to_thread(
            self._repository.list_timed_rosters
        ):
            cycle_key = f"once:{roster.one_off_open_ts}"
            try:
                if (
                    roster.one_off_open_ts is not None
                    and roster.one_off_close_ts is not None
                    and roster.one_off_open_ts
                    <= int(now.timestamp())
                    < roster.one_off_close_ts
                    and roster.last_open_cycle_key != cycle_key
                ):
                    await self.open_scheduled(roster, cycle_key)
                elif (
                    roster.one_off_close_ts is not None
                    and int(now.timestamp()) >= roster.one_off_close_ts
                    and roster.last_close_cycle_key != cycle_key
                ):
                    await self.close_scheduled(roster, cycle_key)
            except (OSError, RuntimeError, sqlite3.Error):
                LOGGER.exception(
                    "Timed roster transition failed roster_id=%s",
                    roster.id,
                )

        for roster in await asyncio.to_thread(
            self._repository.list_scheduled_rosters
        ):
            window = due_window(roster, now)
            if window is None:
                continue
            try:
                if (
                    now < window.closes_at
                    and roster.last_open_cycle_key != window.cycle_key
                ):
                    await self.open_scheduled(roster, window.cycle_key)
                elif (
                    now >= window.closes_at
                    and roster.last_close_cycle_key != window.cycle_key
                ):
                    await self.close_scheduled(roster, window.cycle_key)
            except (OSError, RuntimeError, sqlite3.Error):
                LOGGER.exception(
                    "Scheduled roster transition failed roster_id=%s",
                    roster.id,
                )
