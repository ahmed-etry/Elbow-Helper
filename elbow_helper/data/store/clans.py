"""Clan generations: positive facts, conservative absence and bounded moves."""

from collections import Counter

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from . import FamilyStore
from .people import account, account_name, close_interval
from .projections import (
    CLAN_COUNTER_FIELDS, DONATION_FIELDS, canonical_json, profile_valid, roster_projection,
)


def _payload(response):
    return response.payload if response and response.ok and response.error is None else None


def _error(response):
    return "transport_error" if response and response.error is not None else "fetch_failed"


def _profile(connection, clan_id, payload, at, item, previous):
    # A member list contains personal identities; retain it only in the typed tables.
    metadata = canonical_json({key: value for key, value in payload.items()
                               if key not in CLAN_COUNTER_FIELDS and key != "memberList"})
    last = connection.execute(
        "SELECT metadata_json FROM clan_observations WHERE clan_id=? ORDER BY observed_at DESC LIMIT 1", (clan_id,),
    ).fetchone()
    if last is None or last[0] != metadata:
        connection.execute(
            "INSERT INTO clan_observations VALUES (?,?,?,?,?)", (clan_id, at, metadata, previous, item),
        )
    # Slow metadata changes independently of the hourly wide counter sample.
    last_sample = connection.execute("SELECT MAX(sampled_at) FROM clan_counters WHERE clan_id=?", (clan_id,)).fetchone()[0]
    if last_sample is None or at // 3_600_000_000 > last_sample // 3_600_000_000:
        connection.execute(
            "INSERT INTO clan_counters VALUES (?,?,?,?,?)",
            (clan_id, at, canonical_json({key: payload.get(key) for key in CLAN_COUNTER_FIELDS}),
             canonical_json([key for key in CLAN_COUNTER_FIELDS if key in payload]), item),
        )
    connection.execute("UPDATE clans SET name=? WHERE id=?", (payload["name"], clan_id))


def collect_clans(connection, run, clans, responses, at):
    projections = []
    # Validate all rosters before publishing any location: cross-clan duplicates
    # must never manufacture a transfer based on response ordering.
    for index, clan in enumerate(clans):
        profile = _payload(responses[index])
        valid = profile_valid(profile, clan.tag)
        # Both projections now share a body, so an identity mismatch invalidates
        # its roster too; it must not label another clan's positive observations.
        roster = profile if isinstance(profile, dict) and normalize_player_tag(profile.get("tag")) == clan.tag else None
        rows, complete = roster_projection(roster, profile["members"] if valid else None)
        projections.append([rows, complete, profile if valid else None])
    duplicates = {tag for tag, count in Counter(
        row["tag"] for rows, _, _ in projections for row in rows
    ).items() if count > 1}
    conflicts = set()
    for index, projection in enumerate(projections):
        if any(row["tag"] in duplicates for row in projection[0]):
            conflicts.add(index)
            projection[1] = False
            projection[0] = [row for row in projection[0] if row["tag"] not in duplicates]

    # One bounded transaction across nine clans makes transfers and all item
    # outcomes atomic. There is no network work inside this transaction.
    with sqlite_transaction(connection, immediate=True):
        complete_scopes = []
        for index, clan in enumerate(clans):
            connection.execute(
                "INSERT INTO clans(tag,family_code,name,is_family,wiring_revision) VALUES (?,?,?,1,1) "
                "ON CONFLICT(tag) DO UPDATE SET family_code=excluded.family_code,is_family=1",
                (clan.tag, clan.code, clan.name),
            )
            clan_id = connection.execute("SELECT id FROM clans WHERE tag=?", (clan.tag,)).fetchone()[0]
            response = responses[index]
            rows, complete, profile = projections[index]
            profile_item = FamilyStore.item(connection, run, index * 2)["id"]
            roster_item = FamilyStore.item(connection, run, index * 2 + 1)["id"]
            before = connection.total_changes
            if profile is not None:
                previous = FamilyStore.previous_good(connection, "clan_profile", clan.tag)
                _profile(connection, clan_id, profile, at, profile_item, previous)
            FamilyStore.outcome(
                connection, profile_item, "ok" if profile is not None else "failed", at,
                error=None if profile is not None else "invalid_profile" if _payload(response) is not None else _error(response),
                response=response, count=1 if profile is not None else None,
                complete=profile is not None, written=connection.total_changes - before,
            )
            before = connection.total_changes
            previous = FamilyStore.previous_good(connection, "clan_roster", clan.tag)
            observed = set()
            for row in rows:
                account_id = account(connection, row["tag"], at)
                observed.add(account_id)
                account_name(connection, account_id, row["name"], at, roster_item)
                connection.execute(
                    "INSERT INTO donation_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (account_id, at, clan_id, None, *(row.get(key) for key in DONATION_FIELDS),
                     canonical_json([key for key in DONATION_FIELDS if key in row]), roster_item),
                )
                # Partial rosters retain positive name/counter evidence but do
                # not change any stays, roles, or last-good membership times.
                if not complete:
                    continue
                old = connection.execute(
                    "SELECT * FROM clan_membership WHERE account_id=? AND to_at IS NULL", (account_id,),
                ).fetchone()
                if old and at < old["last_observed_at"]:
                    raise ValueError("out_of_order_observation")
                changed = old is None or old["clan_id"] != clan_id or old["clan_role"] != row["role"]
                lower = previous
                if old and changed:
                    lower = max(value for value in (previous, old["last_observed_at"]) if value is not None)
                    close_interval(connection, "clan_membership", old, at, roster_item,
                                   basis="role_change" if old["clan_id"] == clan_id else "observed_destination")
                if changed:
                    connection.execute(
                        "INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,previous_good_at,"
                        "last_observed_at,source_item_id) VALUES (?,?,?,?,?,?,?)",
                        (account_id, clan_id, row["role"], at, lower, at, roster_item),
                    )
                else:
                    connection.execute("UPDATE clan_membership SET last_observed_at=? WHERE id=?", (at, old["id"]))
            if complete:
                complete_scopes.append((clan_id, observed, roster_item))
            FamilyStore.outcome(
                connection, roster_item, ("ok" if rows else "empty") if complete else "failed", at,
                error=None if complete else "roster_conflict" if index in conflicts else
                "partial_roster" if _payload(response) is not None else _error(response),
                response=response, count=len(rows), complete=complete,
                written=connection.total_changes - before,
            )
        # Destinations above win even when an old clan is unavailable. Only
        # complete source rosters can otherwise close an absent member's stay.
        for clan_id, observed, item in complete_scopes:
            before = connection.total_changes
            for old in connection.execute("SELECT * FROM clan_membership WHERE clan_id=? AND to_at IS NULL", (clan_id,)):
                if old["account_id"] not in observed:
                    close_interval(connection, "clan_membership", old, at, item, basis="complete_roster_absence")
            connection.execute("UPDATE collection_items SET written_count=written_count+? WHERE id=?",
                               (connection.total_changes - before, item))
