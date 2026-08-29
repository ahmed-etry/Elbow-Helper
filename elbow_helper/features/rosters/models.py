"""Typed records used by the roster service and Discord UI."""

from __future__ import annotations

from dataclasses import dataclass
from .config import ROSTER_DISCORD_COLUMN_WIDTH
from .config import ROSTER_PLAYER_COLUMN_WIDTH


@dataclass(frozen=True)
class Roster:
    id: int
    guild_id: int
    name: str
    clan_code: str
    role_id: int | None
    max_members: int
    min_townhall: int | None
    buttons_hidden: bool
    status: str
    schedule_enabled: bool
    schedule_utc_offset: str | None
    open_day: str | None
    open_time: str | None
    close_day: str | None
    close_time: str | None
    one_off_open_ts: int | None
    one_off_close_ts: int | None
    reset_on_open: bool
    active_cycle_id: int | None
    last_open_cycle_key: str | None
    last_close_cycle_key: str | None

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "Roster":
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            clan_code=str(row["clan_code"]),
            role_id=int(row["role_id"]) if row.get("role_id") is not None else None,
            max_members=int(row["max_members"]),
            min_townhall=(
                int(row["min_townhall"])
                if row.get("min_townhall") is not None
                else None
            ),
            buttons_hidden=bool(row.get("buttons_hidden")),
            status=str(row["status"]),
            schedule_enabled=bool(row["schedule_enabled"]),
            schedule_utc_offset=(
                str(row["schedule_utc_offset"])
                if row.get("schedule_utc_offset")
                else None
            ),
            open_day=str(row["open_day"]) if row.get("open_day") is not None else None,
            open_time=str(row["open_time"]) if row.get("open_time") else None,
            close_day=str(row["close_day"]) if row.get("close_day") else None,
            close_time=str(row["close_time"]) if row.get("close_time") else None,
            one_off_open_ts=(
                int(row["one_off_open_ts"])
                if row.get("one_off_open_ts") is not None
                else None
            ),
            one_off_close_ts=(
                int(row["one_off_close_ts"])
                if row.get("one_off_close_ts") is not None
                else None
            ),
            reset_on_open=bool(row["reset_on_open"]),
            active_cycle_id=int(row["active_cycle_id"]) if row.get("active_cycle_id") else None,
            last_open_cycle_key=str(row["last_open_cycle_key"]) if row.get("last_open_cycle_key") else None,
            last_close_cycle_key=str(row["last_close_cycle_key"]) if row.get("last_close_cycle_key") else None,
        )


@dataclass(frozen=True)
class RosterPost:
    roster_id: int
    channel_id: int
    message_id: int

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "RosterPost":
        return cls(
            roster_id=int(row["roster_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]),
        )


@dataclass(frozen=True)
class RosterMember:
    player_tag: str
    discord_user_id: int
    player_name: str
    clan_code: str
    townhall: int
    signed_up_ts: int
    hero_sum: int = 0

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "RosterMember":
        return cls(
            player_tag=str(row["player_tag"]),
            discord_user_id=int(row["discord_user_id"]),
            player_name=str(row["player_name"]),
            clan_code=str(row["clan_code"]),
            townhall=int(row["townhall"] or 0),
            signed_up_ts=int(row["signed_up_ts"]),
            hero_sum=int(row.get("hero_sum") or 0),
        )


@dataclass(frozen=True)
class RosterLayout:
    show_townhall: bool = True
    show_discord: bool = True
    show_clan: bool = True
    player_width: int = ROSTER_PLAYER_COLUMN_WIDTH
    discord_width: int = ROSTER_DISCORD_COLUMN_WIDTH

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "RosterLayout":
        return cls(
            show_townhall=bool(row["show_townhall"]),
            show_discord=bool(row["show_discord"]),
            show_clan=bool(row["show_clan"]),
            player_width=int(row["player_width"]),
            discord_width=int(row["discord_width"]),
        )


@dataclass(frozen=True)
class LinkedAccount:
    player_tag: str
    player_name: str
    clan_code: str
    townhall: int
    hero_sum: int = 0
    hero_levels: tuple[tuple[str, int], ...] = ()
