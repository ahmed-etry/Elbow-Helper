"""SQLite repository for roster definitions, cycles, and account signups."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import sqlite3
import time
from typing import Iterable

from elbow_helper.infrastructure.persistence import sqlite_connection
from elbow_helper.infrastructure.time import fixed_utc_offset_name

from ..config import DB_PATH
from ..config import ROSTER_DISCORD_COLUMN_MAX_WIDTH
from ..config import ROSTER_DISCORD_COLUMN_MIN_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MAX_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MIN_WIDTH
from .migrations import initialize_roster_schema
from ..models import Roster
from ..models import RosterLayout
from ..models import RosterMember
from ..models import RosterPost


SQLITE_BUSY_TIMEOUT_SECONDS = 30.0


class RosterRepository:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._init_db()

    def connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return sqlite_connection(
            self.path,
            timeout_seconds=SQLITE_BUSY_TIMEOUT_SECONDS,
            busy_timeout_ms=int(SQLITE_BUSY_TIMEOUT_SECONDS * 1_000),
        )

    def _init_db(self) -> None:
        with self.connect() as conn:
            initialize_roster_schema(conn)

    @staticmethod
    def _roster(row: sqlite3.Row | None) -> Roster | None:
        return Roster.from_row(dict(row)) if row else None

    def create_roster(
        self,
        *,
        guild_id: int,
        name: str,
        clan_code: str,
        role_id: int | None,
        max_members: int,
        min_townhall: int | None = None,
    ) -> Roster:
        now = int(time.time())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rosters(
                    guild_id, name, clan_code, role_id, max_members, min_townhall,
                    created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    name,
                    clan_code,
                    role_id,
                    max_members,
                    min_townhall,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM rosters WHERE id = ?", (cursor.lastrowid,)).fetchone()
        roster = self._roster(row)
        if roster is None:
            raise RuntimeError("Roster was not created.")
        return roster

    def clone_roster(
        self,
        source_roster_id: int,
        *,
        name: str,
        clan_code: str | None = None,
        role_id: int | None = None,
        max_members: int | None = None,
        min_townhall: int | None = None,
    ) -> Roster:
        """Copy reusable settings without carrying over live roster state."""
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT * FROM rosters WHERE id = ?",
                (source_roster_id,),
            ).fetchone()
            if source is None:
                conn.rollback()
                raise KeyError(source_roster_id)
            copied_minimum = (
                source["min_townhall"]
                if min_townhall is None
                else (min_townhall or None)
            )
            cursor = conn.execute(
                """
                INSERT INTO rosters(
                    guild_id, name, clan_code, role_id, max_members, min_townhall,
                    buttons_hidden, status, schedule_enabled, schedule_utc_offset,
                    open_day, open_time, close_day, close_time, reset_on_open,
                    created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source["guild_id"],
                    name,
                    clan_code if clan_code is not None else source["clan_code"],
                    role_id if role_id is not None else source["role_id"],
                    max_members if max_members is not None else source["max_members"],
                    copied_minimum,
                    source["buttons_hidden"],
                    source["schedule_enabled"],
                    source["schedule_utc_offset"],
                    source["open_day"],
                    source["open_time"],
                    source["close_day"],
                    source["close_time"],
                    source["reset_on_open"],
                    now,
                    now,
                ),
            )
            clone_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO roster_layouts(
                    roster_id, show_townhall, show_discord, show_clan,
                    player_width, discord_width
                )
                SELECT ?, show_townhall, show_discord, show_clan,
                       player_width, discord_width
                FROM roster_layouts
                WHERE roster_id = ?
                """,
                (clone_id, source_roster_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM rosters WHERE id = ?",
                (clone_id,),
            ).fetchone()
        roster = self._roster(row)
        if roster is None:
            raise RuntimeError("Roster was not cloned.")
        return roster

    def get_roster(self, roster_id: int) -> Roster | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rosters WHERE id = ?", (roster_id,)).fetchone()
        return self._roster(row)

    def list_rosters(self, guild_id: int) -> list[Roster]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rosters WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
        return [Roster.from_row(dict(row)) for row in rows]

    def list_all_rosters(self) -> list[Roster]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rosters ORDER BY guild_id, name COLLATE NOCASE, id"
            ).fetchall()
        return [Roster.from_row(dict(row)) for row in rows]

    def get_layout(self, roster_id: int) -> RosterLayout:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM roster_layouts WHERE roster_id = ?",
                (roster_id,),
            ).fetchone()
        return RosterLayout.from_row(dict(row)) if row else RosterLayout()

    def update_layout(
        self,
        roster_id: int,
        *,
        show_townhall: bool | None = None,
        show_discord: bool | None = None,
        show_clan: bool | None = None,
        player_width: int | None = None,
        discord_width: int | None = None,
    ) -> RosterLayout:
        current = self.get_layout(roster_id)
        updated = RosterLayout(
            show_townhall=current.show_townhall if show_townhall is None else show_townhall,
            show_discord=current.show_discord if show_discord is None else show_discord,
            show_clan=current.show_clan if show_clan is None else show_clan,
            player_width=current.player_width if player_width is None else player_width,
            discord_width=current.discord_width if discord_width is None else discord_width,
        )
        if not (
            ROSTER_PLAYER_COLUMN_MIN_WIDTH
            <= updated.player_width
            <= ROSTER_PLAYER_COLUMN_MAX_WIDTH
        ):
            raise ValueError("Invalid player column width.")
        if not (
            ROSTER_DISCORD_COLUMN_MIN_WIDTH
            <= updated.discord_width
            <= ROSTER_DISCORD_COLUMN_MAX_WIDTH
        ):
            raise ValueError("Invalid Discord column width.")
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM rosters WHERE id = ?",
                (roster_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(roster_id)
            conn.execute(
                """
                INSERT INTO roster_layouts(
                    roster_id, show_townhall, show_discord, show_clan,
                    player_width, discord_width
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(roster_id) DO UPDATE SET
                    show_townhall = excluded.show_townhall,
                    show_discord = excluded.show_discord,
                    show_clan = excluded.show_clan,
                    player_width = excluded.player_width,
                    discord_width = excluded.discord_width
                """,
                (
                    roster_id,
                    int(updated.show_townhall),
                    int(updated.show_discord),
                    int(updated.show_clan),
                    updated.player_width,
                    updated.discord_width,
                ),
            )
            conn.commit()
        return updated

    def list_scheduled_rosters(self) -> list[Roster]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rosters WHERE schedule_enabled = 1 ORDER BY id"
            ).fetchall()
        return [Roster.from_row(dict(row)) for row in rows]

    def list_timed_rosters(self) -> list[Roster]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM rosters
                WHERE one_off_open_ts IS NOT NULL AND one_off_close_ts IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        return [Roster.from_row(dict(row)) for row in rows]

    def list_posts(self, roster_id: int | None = None) -> list[RosterPost]:
        with self.connect() as conn:
            if roster_id is None:
                rows = conn.execute(
                    "SELECT * FROM roster_posts ORDER BY created_ts, message_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM roster_posts
                    WHERE roster_id = ?
                    ORDER BY created_ts, message_id
                    """,
                    (roster_id,),
                ).fetchall()
        return [RosterPost.from_row(dict(row)) for row in rows]

    def add_post(self, roster_id: int, channel_id: int, message_id: int) -> RosterPost:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO roster_posts(roster_id, channel_id, message_id, created_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    roster_id = excluded.roster_id,
                    channel_id = excluded.channel_id
                """,
                (roster_id, channel_id, message_id, int(time.time())),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM roster_posts WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Roster post was not registered.")
        return RosterPost.from_row(dict(row))

    def remove_post(self, message_id: int) -> None:
        self.remove_posts((message_id,))

    def remove_posts(self, message_ids: Iterable[int]) -> None:
        ids = tuple(int(message_id) for message_id in message_ids)
        if not ids:
            return
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM roster_posts WHERE message_id = ?",
                ((message_id,) for message_id in ids),
            )
            conn.commit()

    def remove_posts_for_channel(self, channel_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM roster_posts WHERE channel_id = ?", (channel_id,))
            conn.commit()

    def claim_automation_event(
        self,
        roster_id: int,
        cycle_key: str,
        event_key: str,
    ) -> bool:
        """Claim one scheduled action before it is sent to Discord."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO roster_automation_events(
                    roster_id, cycle_key, event_key, claimed_ts
                ) VALUES (?, ?, ?, ?)
                """,
                (roster_id, cycle_key, event_key, int(time.time())),
            )
            conn.commit()
        return cursor.rowcount == 1

    def release_automation_event(
        self,
        roster_id: int,
        cycle_key: str,
        event_key: str,
    ) -> None:
        """Allow a failed Discord send to be retried on the next scheduler pass."""
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM roster_automation_events
                WHERE roster_id = ? AND cycle_key = ? AND event_key = ?
                """,
                (roster_id, cycle_key, event_key),
            )
            conn.commit()

    def update_roster(self, roster_id: int, **values: object) -> Roster:
        allowed = {
            "name", "clan_code", "role_id", "max_members", "min_townhall",
            "buttons_hidden", "status",
            "schedule_enabled", "schedule_utc_offset",
            "open_day", "open_time", "close_day", "close_time", "reset_on_open",
            "one_off_open_ts", "one_off_close_ts",
            "active_cycle_id", "last_open_cycle_key", "last_close_cycle_key",
            "google_sheet_id",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            roster = self.get_roster(roster_id)
            if roster is None:
                raise KeyError(roster_id)
            return roster
        changes["updated_ts"] = int(time.time())
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE rosters SET {assignments} WHERE id = ?",
                (*changes.values(), roster_id),
            )
            conn.commit()
        roster = self.get_roster(roster_id)
        if roster is None:
            raise KeyError(roster_id)
        return roster

    def configure_schedule(
        self,
        roster_id: int,
        *,
        enabled: bool,
        timezone_name: str,
        open_day: str,
        open_time: str,
        close_day: str,
        close_time: str,
        reset_on_open: bool,
    ) -> Roster:
        fixed_timezone = fixed_utc_offset_name(timezone_name)
        if fixed_timezone is None:
            raise ValueError("Invalid schedule timezone.")
        return self.update_roster(
            roster_id,
            schedule_enabled=int(enabled),
            schedule_utc_offset=fixed_timezone,
            open_day=open_day,
            open_time=open_time,
            close_day=close_day,
            close_time=close_time,
            reset_on_open=int(reset_on_open),
        )

    def start_cycle(self, roster_id: int, cycle_key: str) -> tuple[Roster, int | None]:
        now = int(time.time())
        previous_cycle_id: int | None = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT active_cycle_id FROM rosters WHERE id = ?", (roster_id,)).fetchone()
            if row is None:
                raise KeyError(roster_id)
            previous_cycle_id = int(row["active_cycle_id"]) if row["active_cycle_id"] else None
            conn.execute(
                """
                INSERT INTO roster_cycles(roster_id, cycle_key, opened_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(roster_id, cycle_key) DO UPDATE SET
                    opened_ts = excluded.opened_ts,
                    closed_ts = NULL
                """,
                (roster_id, cycle_key, now),
            )
            cycle = conn.execute(
                "SELECT id FROM roster_cycles WHERE roster_id = ? AND cycle_key = ?",
                (roster_id, cycle_key),
            ).fetchone()
            cycle_id = int(cycle["id"])
            conn.execute(
                """
                UPDATE rosters
                SET active_cycle_id = ?, status = 'open', last_open_cycle_key = ?,
                    updated_ts = ?
                WHERE id = ?
                """,
                (cycle_id, cycle_key, now, roster_id),
            )
            conn.commit()
        roster = self.get_roster(roster_id)
        if roster is None:
            raise KeyError(roster_id)
        return roster, previous_cycle_id

    def close_cycle(self, roster_id: int, cycle_key: str | None = None) -> Roster:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT active_cycle_id, last_open_cycle_key FROM rosters WHERE id = ?",
                (roster_id,),
            ).fetchone()
            if row is None:
                raise KeyError(roster_id)
            active_cycle_id = row["active_cycle_id"]
            if active_cycle_id:
                conn.execute(
                    "UPDATE roster_cycles SET closed_ts = ? WHERE id = ?",
                    (now, active_cycle_id),
                )
            close_key = cycle_key or row["last_open_cycle_key"]
            conn.execute(
                """
                UPDATE rosters SET status = 'closed', last_close_cycle_key = ?,
                    updated_ts = ? WHERE id = ?
                """,
                (close_key, now, roster_id),
            )
            conn.commit()
        roster = self.get_roster(roster_id)
        if roster is None:
            raise KeyError(roster_id)
        return roster

    def ensure_manual_cycle(self, roster_id: int) -> Roster:
        roster = self.get_roster(roster_id)
        if roster is None:
            raise KeyError(roster_id)
        if roster.active_cycle_id:
            return self.update_roster(roster_id, status="open")
        key = f"manual-{int(time.time())}"
        return self.start_cycle(roster_id, key)[0]

    def list_members(self, roster_id: int, cycle_id: int | None) -> list[RosterMember]:
        if cycle_id is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT player_tag, discord_user_id, player_name, clan_code, townhall,
                       signed_up_ts, hero_sum
                FROM roster_members
                WHERE roster_id = ? AND cycle_id = ?
                ORDER BY townhall DESC, hero_sum DESC,
                         player_name COLLATE NOCASE, player_tag
                """,
                (roster_id, cycle_id),
            ).fetchall()
        return [RosterMember.from_row(dict(row)) for row in rows]

    def list_members_for_user(
        self,
        roster_ids: Iterable[int],
        discord_user_id: int,
    ) -> dict[int, list[RosterMember]]:
        ids = tuple(dict.fromkeys(int(roster_id) for roster_id in roster_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT members.roster_id, members.player_tag,
                       members.discord_user_id, members.player_name,
                       members.clan_code, members.townhall,
                       members.signed_up_ts, members.hero_sum
                FROM roster_members AS members
                JOIN rosters AS rosters
                  ON rosters.id = members.roster_id
                 AND rosters.active_cycle_id = members.cycle_id
                WHERE members.roster_id IN ({placeholders})
                  AND members.discord_user_id = ?
                ORDER BY members.roster_id, members.townhall DESC,
                         members.hero_sum DESC,
                         members.player_name COLLATE NOCASE,
                         members.player_tag
                """,
                (*ids, discord_user_id),
            ).fetchall()
        result: dict[int, list[RosterMember]] = {}
        for row in rows:
            result.setdefault(int(row["roster_id"]), []).append(
                RosterMember.from_row(dict(row))
            )
        return result

    def refresh_member_snapshots(
        self,
        roster_id: int,
        cycle_id: int | None,
        accounts: dict[str, dict[str, object]],
    ) -> int:
        if cycle_id is None or not accounts:
            return 0
        updated = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for player_tag, account in accounts.items():
                cursor = conn.execute(
                    """
                    UPDATE roster_members
                    SET player_name = ?,
                        clan_code = ?,
                        townhall = CASE WHEN ? > 0 THEN ? ELSE townhall END,
                        hero_sum = CASE WHEN ? > 0 THEN ? ELSE hero_sum END
                    WHERE roster_id = ? AND cycle_id = ? AND player_tag = ?
                    """,
                    (
                        str(account.get("player_name") or player_tag),
                        str(account.get("clan_code") or ""),
                        int(account.get("townhall") or 0),
                        int(account.get("townhall") or 0),
                        int(account.get("hero_sum") or 0),
                        int(account.get("hero_sum") or 0),
                        roster_id,
                        cycle_id,
                        player_tag,
                    ),
                )
                updated += int(cursor.rowcount)
            conn.commit()
        return updated

    def add_members(
        self,
        roster_id: int,
        cycle_id: int,
        discord_user_id: int,
        accounts: list[dict[str, object]],
        max_members: int,
        min_townhall: int | None = None,
    ) -> tuple[int, int]:
        now = int(time.time())
        added = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = int(conn.execute(
                "SELECT COUNT(*) FROM roster_members WHERE roster_id = ? AND cycle_id = ?",
                (roster_id, cycle_id),
            ).fetchone()[0])
            for account in accounts:
                if count >= max_members:
                    break
                if (
                    min_townhall is not None
                    and int(account.get("townhall") or 0) < min_townhall
                ):
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO roster_members(
                        roster_id, cycle_id, player_tag, discord_user_id,
                        player_name, clan_code, townhall, hero_sum, signed_up_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        roster_id,
                        cycle_id,
                        str(account["player_tag"]),
                        discord_user_id,
                        str(account["player_name"]),
                        str(account.get("clan_code") or ""),
                        int(account.get("townhall") or 0),
                        int(account.get("hero_sum") or 0),
                        now,
                    ),
                )
                if cursor.rowcount:
                    added += 1
                    count += 1
            conn.commit()
        return added, count

    def remove_members(
        self,
        roster_id: int,
        cycle_id: int,
        *,
        discord_user_id: int,
        player_tags: list[str],
    ) -> int:
        if not player_tags:
            return 0
        placeholders = ", ".join("?" for _ in player_tags)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM roster_members
                WHERE roster_id = ? AND cycle_id = ? AND discord_user_id = ?
                    AND player_tag IN ({placeholders})
                """,
                (roster_id, cycle_id, discord_user_id, *player_tags),
            )
            conn.commit()
        return int(cursor.rowcount)

    def clear_members(self, roster_id: int, cycle_id: int | None) -> list[int]:
        if cycle_id is None:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT discord_user_id FROM roster_members WHERE roster_id = ? AND cycle_id = ?",
                (roster_id, cycle_id),
            ).fetchall()
            conn.execute(
                "DELETE FROM roster_members WHERE roster_id = ? AND cycle_id = ?",
                (roster_id, cycle_id),
            )
            conn.commit()
        return [int(row["discord_user_id"]) for row in rows]

    def member_has_signup(self, roster_id: int, cycle_id: int | None, discord_user_id: int) -> bool:
        if cycle_id is None:
            return False
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM roster_members
                WHERE roster_id = ? AND cycle_id = ? AND discord_user_id = ?
                LIMIT 1
                """,
                (roster_id, cycle_id, discord_user_id),
            ).fetchone()
        return row is not None

    def role_still_needed(self, role_id: int, discord_user_id: int, *, excluding_roster_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM roster_members m
                JOIN rosters r ON r.id = m.roster_id AND r.active_cycle_id = m.cycle_id
                WHERE r.role_id = ? AND m.discord_user_id = ? AND r.id != ?
                LIMIT 1
                """,
                (role_id, discord_user_id, excluding_roster_id),
            ).fetchone()
        return row is not None

    def role_has_signup(self, role_id: int, discord_user_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM roster_members m
                JOIN rosters r ON r.id = m.roster_id AND r.active_cycle_id = m.cycle_id
                WHERE r.role_id = ? AND m.discord_user_id = ?
                LIMIT 1
                """,
                (role_id, discord_user_id),
            ).fetchone()
        return row is not None

    def delete_roster(self, roster_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM rosters WHERE id = ?", (roster_id,))
            conn.commit()
