"""Read-only account evidence for complete member-group reports."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag


LOCATION_CONCURRENCY = 4
LOCATION_REFRESH_SECONDS = 20.0
ACCOUNT_LOCATION_FIELDS = frozenset({
    "player_tag", "player_name", "primary", "clan_tag", "clan_code",
    "clan_name", "clan_role", "in_clan", "location_status", "checked_at",
})
ACCOUNT_LOCATION_STATUSES = frozenset({
    "unknown", "last_known", "refresh_failed", "refreshed",
})


@dataclass(frozen=True, slots=True)
class AccountTagEvidence:
    player_tag: str
    linked_member_id: int | None
    linked_player_name: str | None
    last_known_clan_code: str | None
    location_status: str
    observed_player_name: str | None
    observed_clan_code: str | None
    observed_clan_tag: str | None
    observed_townhall: int | None


@dataclass(frozen=True, slots=True)
class AccountTagEvidenceSnapshot:
    observed_at: str | None
    locations_complete: bool
    accounts: tuple[AccountTagEvidence, ...]


@dataclass(frozen=True, slots=True)
class AccountOwnershipEvidence:
    player_tag: str
    linked_member_id: int | None
    linked_player_name: str | None


@dataclass(frozen=True, slots=True)
class AccountOwnershipEvidenceSnapshot:
    observed_at: str
    accounts: tuple[AccountOwnershipEvidence, ...]


def account_ownership_evidence(
    account_links: Any, player_tags: Iterable[str], *, observed_at: datetime | None = None,
) -> AccountOwnershipEvidenceSnapshot:
    """Read current tag ownership in one repository transaction, without a refresh."""
    tags = tuple(dict.fromkeys(str(value) for value in player_tags))
    links = account_links.get_links_by_tags(tags)
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return AccountOwnershipEvidenceSnapshot(
        observed_at=now.astimezone(timezone.utc).isoformat(),
        accounts=tuple(AccountOwnershipEvidence(
            player_tag=tag,
            linked_member_id=int(links[tag]["discord_user_id"]) if links.get(tag) else None,
            linked_player_name=(
                str(links[tag].get("player_name_last_seen") or "") or None
                if links.get(tag) else None
            ),
        ) for tag in tags),
    )


def account_tag_evidence(account_links: Any, player_tags: Iterable[str]) -> AccountTagEvidenceSnapshot:
    """Read ownership consistently and label missing location evidence honestly."""
    tags = tuple(dict.fromkeys(str(value) for value in player_tags))
    links = account_links.get_links_by_tags(tags)
    status = account_links.get_player_locations_snapshot(tags)
    complete = bool(status.get("complete"))
    locations = status.get("locations") or {}
    location_statuses = status.get("location_statuses") or {}
    accounts = []
    for tag in tags:
        link = links.get(tag) or {}
        location = locations.get(tag)
        if location is not None:
            location_status = str(location_statuses.get(tag) or "observed_family_clan")
        elif complete:
            location_status = "not_in_complete_family_snapshot"
        else:
            location_status = "location_unknown"
        accounts.append(AccountTagEvidence(
            player_tag=tag,
            linked_member_id=int(link["discord_user_id"]) if link else None,
            linked_player_name=str(link.get("player_name_last_seen") or "") or None,
            last_known_clan_code=str(link.get("last_seen_clan_code") or "") or None,
            location_status=location_status,
            observed_player_name=(str(location.get("player_name") or "") or None) if location else None,
            observed_clan_code=(str(location.get("clan_code") or "") or None) if location else None,
            observed_clan_tag=(str(location.get("clan_tag") or "") or None) if location else None,
            observed_townhall=int(location.get("townhall") or 0) or None if location else None,
        ))
    return AccountTagEvidenceSnapshot(
        observed_at=str(status.get("observed_at")) if status.get("observed_at") else None,
        locations_complete=complete, accounts=tuple(accounts),
    )


async def member_account_evidence(
    account_links: Any,
    member_ids: Iterable[int],
    *,
    refresh: bool,
) -> dict[int, list[dict[str, Any]]]:
    """Preserve every account when refreshing fails or the time budget ends."""
    links = await asyncio.to_thread(account_links.get_links_for_members, tuple(member_ids))
    by_tag: dict[str, dict[str, Any]] = {}
    result: dict[int, list[dict[str, Any]]] = {}
    for member_id, rows in links.items():
        result[member_id] = []
        for row in rows:
            tag = str(row["player_tag"])
            location = account_links.get_player_location(tag) or {}
            clan_tag = location.get("clan_tag") or row.get("last_seen_clan_tag") or None
            clan_code = location.get("clan_code") or row.get("last_seen_clan_code") or None
            account = {
                "player_tag": tag,
                "player_name": location.get("player_name") or row.get("player_name_last_seen") or tag,
                "primary": bool(row.get("is_primary")),
                "clan_tag": clan_tag,
                "clan_code": clan_code,
                "clan_name": CLANS[clan_code].name if clan_code in CLANS else None,
                "clan_role": location.get("role") or row.get("last_seen_role") or None,
                "in_clan": True if clan_tag else None,
                "location_status": "last_known" if clan_tag else "unknown",
                "checked_at": None,
            }
            result[member_id].append(account)
            by_tag[tag] = account
    if not refresh or not by_tag:
        return result

    refreshed = await _refresh_account_locations(
        account_links, tuple(by_tag.values()),
    )
    refreshed_by_tag = {row["player_tag"]: row for row in refreshed}
    for rows in result.values():
        rows[:] = [refreshed_by_tag[row["player_tag"]] for row in rows]
    return result


async def refresh_account_locations(
    account_links: Any,
    accounts: Iterable[dict[str, Any]],
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], ...]:
    """Refresh one bounded account batch without mutating retained source rows."""

    rows = tuple(accounts)
    if len(rows) > 25:
        raise ValueError("Account refresh batch exceeds 25 rows")
    return await _refresh_account_locations(
        account_links, rows, timeout_seconds=timeout_seconds,
    )


def validate_account_location_rows(
    accounts: Iterable[dict[str, Any]], *, maximum: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Validate bounded persisted refresh inputs at the account-link boundary."""

    rows = tuple(deepcopy(row) for row in accounts)
    if maximum is not None and len(rows) > maximum:
        raise ValueError("Account refresh input exceeds its row bound")
    tags: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ACCOUNT_LOCATION_FIELDS:
            raise ValueError("Invalid account refresh row")
        tag = row["player_tag"]
        if normalize_player_tag(tag) != tag or tag in tags:
            raise ValueError("Invalid account refresh tag")
        tags.add(tag)
        if (
            not isinstance(row["player_name"], str)
            or not row["player_name"]
            or len(row["player_name"]) > 200
            or type(row["primary"]) is not bool
            or row["location_status"] not in ACCOUNT_LOCATION_STATUSES
            or row["in_clan"] is not None
            and type(row["in_clan"]) is not bool
        ):
            raise ValueError("Invalid account refresh row")
        for key in (
            "clan_tag", "clan_code", "clan_name", "clan_role", "checked_at",
        ):
            value = row[key]
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 200
            ):
                raise ValueError("Invalid account refresh row")
    return rows


async def _refresh_account_locations(
    account_links: Any,
    accounts: Iterable[dict[str, Any]],
    *,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], ...]:
    rows = validate_account_location_rows(accounts)
    if not rows:
        return ()
    if timeout_seconds is None:
        timeout_seconds = LOCATION_REFRESH_SECONDS
    if timeout_seconds <= 0 or timeout_seconds > LOCATION_REFRESH_SECONDS:
        raise ValueError("Invalid account refresh timeout")
    by_tag = {str(row["player_tag"]): row for row in rows}
    if len(by_tag) != len(rows):
        raise ValueError("Account refresh batch contains duplicate tags")

    # Initialise every pending account before starting network work. A timeout
    # leaves explicit unresolved rows, never an incomplete member report.
    for account in by_tag.values():
        account["location_status"] = "refresh_failed"
    tags = iter(by_tag)
    clans_by_tag = {clan.tag: clan for clan in CLANS.values()}

    async def worker() -> None:
        for tag in tags:
            if normalize_player_tag(tag) is None:
                continue
            response = await account_links.clash_client.get(
                f"/players/{encode_clash_tag(tag)}", attempts=1, timeout_seconds=5.0,
            )
            payload = response.payload_object
            if not response.ok or not payload or normalize_player_tag(payload.get("tag")) != tag:
                continue
            clan = payload.get("clan")
            if clan is not None and (not isinstance(clan, dict) or not clan.get("tag")):
                continue
            clan = clan or {}
            clan_tag = clan.get("tag")
            family_clan = clans_by_tag.get(clan_tag)
            by_tag[tag].update(
                player_name=payload.get("name") or by_tag[tag]["player_name"],
                clan_tag=clan_tag,
                clan_code=family_clan.code if family_clan else None,
                clan_name=clan.get("name"),
                clan_role=payload.get("role"),
                in_clan=bool(clan_tag),
                location_status="refreshed",
                checked_at=datetime.now(timezone.utc).isoformat(),
            )

    tasks = [asyncio.create_task(worker()) for _ in range(min(LOCATION_CONCURRENCY, len(by_tag)))]
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return rows
