"""Typed people selection over guarded links and shared dated record reads."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from typing import Any, Literal

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.configuration.clans import CLANS

from elbow_helper.data.store.link_state import LinkConflict
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
from ..access import require_evidence_access
from ..read_cache import stored_read
from ..research.history import search_date


@dataclass(frozen=True)
class PeopleSelection:
    """Account selectors never expand to the other accounts of their owners."""

    direction: Literal['members', 'accounts', 'all']
    values: tuple[int, ...] | tuple[str, ...] = ()


@dataclass
class PeopleLinks:
    by_member: dict[int, list[dict[str, Any]]]
    by_tag: dict[str, dict[str, Any] | None]
    status: Literal['available', 'missing_setup', 'source_unavailable']


@dataclass(frozen=True)
class RosterPopulation:
    player_tags: tuple[str, ...]
    basis: str
    season: str | None
    observed_at: str | None
    seasonal: bool


class PeopleAdapter:
    """The agent's current links, aliases, population and dated identity joins.

    Current links use PeopleReads' guarded projection, including completed
    local operations immediately. Standalone feature contexts can supply the
    same projection through LinkFacade; that synchronous adapter is threaded.
    """

    def __init__(self, context: Any):
        self.context = context

    async def links(self, selection: PeopleSelection) -> PeopleLinks:
        context = self.context
        await require_evidence_access(context)
        members, tags = {}, {}
        if selection.direction == 'members':
            members = dict.fromkeys(selection.values)
            members = {identifier: [] for identifier in members}
        elif selection.direction == 'accounts':
            tags = dict.fromkeys(selection.values)
        elif selection.direction != 'all':
            raise ValueError('Unknown people selection')
        if context.account_links is None:
            return PeopleLinks(members, tags, 'missing_setup')
        if selection.direction != 'all' and not selection.values:
            return PeopleLinks(members, tags, 'available')
        try:
            reader = getattr(getattr(context, 'bot', None), 'people_reads', None)
            if getattr(reader, 'link_service', None) is not None:
                snapshot = await reader.current_links()
                if snapshot['coverage'].get('unavailable'):
                    return PeopleLinks({}, {}, 'source_unavailable')
                wanted = set(selection.values)
                for row in snapshot['rows']:
                    if (selection.direction == 'members' and row['discord_user_id'] not in wanted
                            or selection.direction == 'accounts' and row['player_tag'] not in wanted):
                        continue
                    tags[row['player_tag']] = row
                    members.setdefault(row['discord_user_id'], []).append(row)
            elif selection.direction == 'members':
                members = await asyncio.to_thread(context.account_links.get_links_for_members, list(selection.values))
                tags = {row['player_tag']: {**row, 'discord_user_id': identifier}
                        for identifier, rows in members.items() for row in rows if row.get('player_tag')}
            else:
                tags = (await asyncio.to_thread(context.account_links.get_all_links)
                        if selection.direction == 'all' else
                        await asyncio.to_thread(context.account_links.get_links_by_tags, list(selection.values)))
                for tag, row in tags.items():
                    if row:
                        members.setdefault(row['discord_user_id'], []).append({**row, 'player_tag': tag})
        except (OSError, sqlite3.Error, TimeoutError, LinkConflict, SQLiteMigrationError):
            return PeopleLinks({}, {}, 'source_unavailable')
        await require_evidence_access(context)
        return PeopleLinks(members, tags, 'available')

    async def directory_names(self) -> list[dict[str, Any]] | None:
        """Use the shared equivalent of the CWL/stay directory union."""
        context = self.context
        reader = getattr(getattr(context, 'bot', None), 'people_reads', None)
        if callable(getattr(reader, 'directory_names', None)):
            result = await record(context, 'people_reads', 'directory_names')
            return None if result is None else result['rows']
        if context.cwl_queries is None:
            return None
        await require_evidence_access(context)
        try:
            result = await asyncio.to_thread(context.cwl_queries.account_names)
        except (OSError, sqlite3.Error, TimeoutError):
            return None
        await require_evidence_access(context)
        return list(result.values()) if isinstance(result, dict) else result

    async def population(self, code: str, basis: str, season: str | None,
                         current: dict[str, Any], *, seasonal_reader) -> RosterPopulation:
        """Select accounts before profile enrichment; parked accounts stay out."""
        if basis == 'seasonal_cwl' or CLANS[code].cwl_only and basis == 'community':
            source = await seasonal_reader(self.context, code, season)
            return RosterPopulation(tuple(source[0]), source[2], source[1],
                                    source[3] if len(source) > 3 else None, True)
        return RosterPopulation(tuple(row['tag'] for row in current.get('data', {}).get('memberList', [])),
                                'current_in_game', season, current.get('observed_at'), False)


async def record(context, source, method, *args, **kwargs):
    reader = getattr(getattr(context, 'bot', None), source, None)
    if reader is None:
        return None
    await require_evidence_access(context)
    try:
        result = await getattr(reader, method)(*args, **kwargs)
    except (OSError, sqlite3.Error, TimeoutError, LinkConflict, SQLiteMigrationError):
        return None
    await require_evidence_access(context)
    return result


async def account_histories(context, tags):
    """Dated ownership, clan stays and names; never current profile fields."""
    histories = {}
    for start in range(0, len(tags), 200):
        batch = await record(context, 'people_reads', 'account_history', tags[start:start + 200])
        if batch is not None:
            histories.update((row['player_tag'], row['record_history']) for row in batch['rows']
                             if row['record_history'] is not None)
    return histories


async def stored_profiles(context, tags):
    """Each account's latest stored profile summary; None when the record cannot be read."""
    profiles = {}
    for start in range(0, len(tags), 200):
        batch = await record(context, 'people_reads', 'account_summary', tags[start:start + 200])
        if batch is None:
            return None
        profiles.update((row['player_tag'], row) for row in batch['rows'])
    return profiles


def observed(value):
    return None if value is None else datetime.fromtimestamp(value / 1_000_000, timezone.utc).isoformat()


async def aliases(context, queries):
    """Feed recorded names into the existing live name resolver."""
    accounts = {}
    for query in queries:
        offset = 0
        while True:
            batch = await record(context, 'people_reads', 'resolve', query, offset=offset)
            if batch is None:
                break
            for row in batch['rows']:
                if row['kind'] == 'account':
                    accounts[row['player_tag']] = row['names']
            offset = batch['next_offset']
            if offset is None:
                break
    return accounts


async def read_people_links(context, arguments, *, maximum=200):
    await require_evidence_access(context)
    members = arguments.get("member_ids")
    if (not isinstance(members, list) or not 1 <= len(members) <= maximum
            or any(type(value) is not int or value <= 0 for value in members)):
        return {"error": "Choose between 1 and 200 member IDs."}
    if context.account_links is None:
        return {"error": "Account links are not available.", "cause": "missing_setup"}
    members = list(dict.fromkeys(members))
    selected = await PeopleAdapter(context).links(PeopleSelection('members', tuple(members)))
    if selected.status != 'available':
        return {'error': 'Account links are not available.', 'cause': selected.status}
    links = selected.by_member
    await require_evidence_access(context)
    rows = []
    for member_id in members:
        for link in links.get(member_id, ()):
            tag = normalize_player_tag(link.get("player_tag"))
            if tag is not None:
                rows.append({"member_id": member_id, "player_tag": tag,
                             "player_name": link.get("player_name_last_seen")})
    return {"source": "bot_account_links", "accounts": rows,
            "member_ids": members, "unlinked_member_ids": [value for value in members
                if not any(row["member_id"] == value for row in rows)],
            "account_count": len(rows), "member_count": len(members),
            "field_meanings": {"member_id": "Discord member owning the linked account",
                               "player_tag": "one Clash account; a member may own several",
                               "player_name": "last recorded name, not a current profile"}}


async def seasonal_tags(context, code, season):
    if context.cwl_queries:
        if callable(getattr(context.cwl_queries, 'seasonal_cohort', None)):
            try:
                source = await stored_read(context, context.cwl_queries, 'seasonal_cohort',
                                           clan_code=code, season=season)
            except (OSError, RuntimeError, sqlite3.Error):
                source = None
            if source is not None:
                return (source['player_tags'], source['season'], 'league_group_cohort', source['observed_at'])
        snapshot = await stored_read(context, context.cwl_queries, 'performance', history_limit=1,
                                            season=season, clan_code=code)
        if not snapshot.rows and not snapshot.seasons:
            return [], season, 'season_cohort_unavailable'
        return sorted({row.player_tag for row in snapshot.rows}), \
               season or (snapshot.seasons[0] if snapshot.seasons else None), 'stored_completed_cwl_participants'
    return [], season, 'season_cohort_unavailable'


def league_key(value):
    return re.sub(r"[\s_-]+", "", re.sub(r"\bleague\b", "", value.casefold()))


def standing_rows(accounts, profiles, league):
    """Project the same selected accounts and latest stored tiers for cohort reads."""
    from ..datasets.clash import RANKED_LEAGUE_FIELDS
    result = []
    for account in accounts:
        item = profiles.get(account["player_tag"], {})
        fields = item.get("fields", {})
        tier = item.get("ranked_league") or {}
        tier = tier if isinstance(tier, dict) else {}
        legacy = item.get("legacy_league") or {}
        name = tier.get("name")
        result.append({**account, "status": item.get("coverage", {}).get("status", "unavailable"),
                       "observed_at": observed(item.get("field_observed_at", {}).get("leagueTier")),
                       "ranked_league": name,
                       "leagueTier": {key: tier[key] for key in RANKED_LEAGUE_FIELDS if key in tier},
                       "legacy_league": legacy.get("name") if isinstance(legacy, dict) else None,
                       "trophies": fields.get("trophies"), "best_trophies": fields.get("bestTrophies"),
                       "matches": (league_key(name).startswith(league) if isinstance(name, str) else None)})
    return result


STAY_LIMIT = 100_000
GAP_SECONDS = 48 * 3600
# A stay that starts this soon after the last hourly capture of the account's copied old stay continues from it.
TRANSFER_SECONDS = 2 * 3600
SECOND = 1_000_000


def membership_events(accounts, after, clan_codes):
    """Arrivals in the window from each account's stays (oldest first) seen inside the history bounds."""
    events = []
    for account in accounts:
        previous = None
        for stay in account["stays"]:
            # A record stay ends at the roster that first missed the account; an hourly copied one at its last capture.
            continued = previous is not None and (
                (previous["to_at"] is not None and stay["from_at"] <= previous["to_at"])
                or (previous["basis"] == "observed_hourly"
                    and stay["from_at"] - previous["last_seen_at"] <= TRANSFER_SECONDS * SECOND))
            if continued and previous["clan_code"] == stay["clan_code"]:
                # The same stay, carried from the old file's copy into the record.
                previous = {**stay, "from_at": previous["from_at"], "first_seen_at": previous["first_seen_at"]}
                continue
            if stay["from_at"] >= after and (not clan_codes or stay["clan_code"] in clan_codes):
                kind = ("family_transfer" if continued else
                        "returning_observation" if previous is not None else "first_observed")
                events.append({
                    "player_tag": account["player_tag"], "player_name": stay.get("player_name"),
                    "clan_code": stay["clan_code"], "kind": kind, "observed_at": observed(stay["from_at"]),
                    "previous_observation_at": observed(previous["last_seen_at"]) if previous else None,
                    "previous_clan_code": previous["clan_code"] if continued else None,
                    "prior_membership_observed_at": observed(previous["first_seen_at"]) if previous else None,
                    "arrival_time_known": False, "first_ever_membership_known": False,
                    "sighting_gap": previous is None or stay["from_at"] - previous["last_seen_at"] > GAP_SECONDS * SECOND,
                })
            previous = stay
    return sorted(events, key=lambda row: (row["observed_at"], row["player_tag"]), reverse=True)


async def read_membership_history(context, arguments):
    await require_evidence_access(context)
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        history_after = search_date(arguments.get("history_after")) or (
            max(after - timedelta(days=30), before - timedelta(days=90)) if after else None)
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        codes = arguments.get("clan_codes", [])
        if (after is None or history_after is None or not history_after <= after < before
                or before - history_after > timedelta(days=90)
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25
                or not isinstance(codes, list) or len(codes) > 20 or any(code not in CLANS for code in codes)):
            raise ValueError("Invalid membership selection")
    except (ValueError, TypeError, AttributeError):
        return {"error": "Use ISO dates within ninety days, valid clan codes and a page of 1..25.",
                "cause": "invalid_arguments"}
    start, end = int(history_after.timestamp() * SECOND), int(before.timestamp() * SECOND)
    snapshot = await record(context, "stay_reads", "movements", after=start, before=end,
                                            limit=STAY_LIMIT)
    if snapshot is None:
        return {"error": "Membership observations could not be read within their bounds.",
                "cause": "source_unavailable"}
    if snapshot["truncated"]:
        return {"error": "Membership observations exceed the row bound; narrow the window.",
                "cause": "source_unavailable"}
    for account in snapshot["rows"]:
        # Only what was seen inside the history bounds counts as an earlier observation.
        account["stays"] = [{**stay, "first_seen_at": max(stay["first_seen_at"], start)} for stay in account["stays"]]
    events = membership_events(snapshot["rows"], int(after.timestamp() * SECOND), codes)
    await require_evidence_access(context)
    selected = events[offset:offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(events) else None
    seen = [stay[key] for account in snapshot["rows"] for stay in account["stays"]
            for key in ("first_seen_at", "last_seen_at")]
    return {
        "source": "family_clan_stays", "events": selected,
        "matching_rows": len(events), "next_offset": next_offset, "truncated": False,
        "coverage": {
            "requested_after": after.isoformat(), "requested_before": before.isoformat(),
            "history_after": history_after.isoformat(), "before_exclusive": True,
            "observed_after": observed(min(seen)) if seen else None,
            "observed_before": observed(max(seen)) if seen else None,
            "stay_count": sum(len(account["stays"]) for account in snapshot["rows"]),
            "history_complete": False, "first_ever_membership_known": False,
        },
        "limitations": {"observations_only": True, "arrivals_between_observations": True,
                        "earlier_membership_outside_window_unknown": True,
                        "before_record_hourly_observations": True},
        "flags": {"status": "partial", "truncated": False, "limits": ["bounded_membership_observations"]},
    }
