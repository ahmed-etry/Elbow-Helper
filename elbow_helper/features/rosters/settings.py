"""Shared roster setup validation and completion messages."""

from typing import Any

from .config import MAX_ROSTER_MEMBERS

ROSTER_NAME_CONFLICT = "A roster with that name already exists."
ROSTER_ACCOUNT_LIMIT = "Choose between 1 and {maximum} accounts."


def clean_roster_name(value: str) -> str | None:
    cleaned = " ".join(value.split())
    return cleaned if 1 <= len(cleaned) <= 100 else None


def validate_roster_setup(*, name: str | None = None, max_members: int | None = None,
                          min_townhall: int | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if name is not None:
        cleaned = clean_roster_name(name)
        if cleaned is None:
            raise ValueError("Enter a roster name between 1 and 100 characters.")
        values["name"] = cleaned
    if max_members is not None:
        try:
            maximum = int(max_members)
        except (TypeError, ValueError) as error:
            raise ValueError(ROSTER_ACCOUNT_LIMIT.format(maximum=MAX_ROSTER_MEMBERS)) from error
        if not 1 <= maximum <= MAX_ROSTER_MEMBERS:
            raise ValueError(ROSTER_ACCOUNT_LIMIT.format(maximum=MAX_ROSTER_MEMBERS))
        values["max_members"] = maximum
    if min_townhall is not None:
        try:
            minimum = int(min_townhall)
        except (TypeError, ValueError) as error:
            raise ValueError("That roster is unavailable.") from error
        if minimum < 0:
            raise ValueError("That roster is unavailable.")
        values["min_townhall"] = minimum
    return values


def validate_roster_clone_settings(settings: dict[str, Any]) -> None:
    if settings["schedule_enabled"] and not all(settings[key] for key in (
        "open_day", "open_time", "close_day", "close_time", "schedule_utc_offset",
    )):
        raise ValueError("That roster is unavailable.")


async def require_roster_name_available(workflow: Any, guild_id: int, name: str) -> None:
    if not await workflow.roster_name_available(guild_id, name):
        raise ValueError(ROSTER_NAME_CONFLICT)


async def require_guild_roster(workflow: Any, guild_id: int, identifier: Any):
    try:
        roster_id = int(identifier)
    except (TypeError, ValueError) as error:
        raise ValueError("That roster is unavailable.") from error
    roster = await workflow.get_roster(roster_id)
    if roster is None or roster.guild_id != guild_id:
        raise ValueError("That roster is unavailable.")
    return roster


def roster_setup_confirmation(operation: str, roster: Any, *, source: Any = None) -> str:
    messages = {
        "create": "Created **{name}**.",
        "clone": "Created **{name}** from **{source}**.",
        "delete": "Deleted **{name}**.",
        "edit": "Updated **{name}**.",
    }
    return messages[operation].format(name=roster.name, source=source.name if source else "")
