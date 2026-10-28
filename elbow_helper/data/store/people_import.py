"""All-or-nothing people import and independently checked clan generations."""

import json

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .api_fields import unpack
from .legacy_links import TOPIC, BootstrapRequired, ImportMismatch, snapshot
from .link_state import apply_rows, check_mapping, receipt
from .projections import canonical_json

ROSTER_MAX_AGE_US = 30 * 60 * 1_000_000


def roster_checks(connection, at, checks):
    clans = tuple(CLANS.values())
    if len(clans) != 9:
        raise ImportMismatch("expected_nine_clans", checks)
    if connection.execute("SELECT 1 FROM clan_membership WHERE to_at IS NULL "
                          "GROUP BY account_id HAVING COUNT(*)>1").fetchone():
        raise ImportMismatch("duplicate_current_location", checks)
    result, runs, seen = {}, set(), set()
    for clan in clans:
        item = connection.execute(
            "SELECT * FROM collection_items WHERE source_kind='clan_roster' AND scope_id=? "
            "ORDER BY COALESCE(fetched_at,started_at) DESC,id DESC LIMIT 1", (clan.tag,),
        ).fetchone()
        if (item is None or item["status"] not in ("ok", "empty") or item["fetched_at"] is None
                or not 0 <= at - item["fetched_at"] <= ROSTER_MAX_AGE_US
                or not json.loads(item["completeness_json"]).get("complete")):
            checks["clans"] = result
            raise BootstrapRequired("missing_or_stale_roster:" + clan.code, checks)
        runs.add(item["run_id"])
        profile = connection.execute("SELECT * FROM collection_items WHERE run_id=? AND scope_id=? "
                                     "AND source_kind='clan_profile'", (item["run_id"], clan.tag)).fetchone()
        if (profile is None or profile["status"] != "ok" or profile["fetched_at"] != item["fetched_at"]
                or not json.loads(profile["completeness_json"]).get("complete")):
            raise BootstrapRequired("missing_roster_profile:" + clan.code, checks)
        identity = connection.execute("SELECT family_code,is_family FROM clans WHERE tag=?", (clan.tag,)).fetchone()
        if identity is None or identity["family_code"] != clan.code or identity["is_family"] != 1:
            raise ImportMismatch("clan_identity_mismatch:" + clan.code, checks)
        record = connection.execute("SELECT state_z FROM api_records WHERE source_kind='clan_profile' AND scope_id=?",
                                    (clan.tag,)).fetchone()
        if record is None:
            raise BootstrapRequired("missing_roster_projection:" + clan.code, checks)
        paths, state = unpack(record[0])
        roster = {}
        for slot, value in state.items():
            parts = [part.replace("~1", "/").replace("~0", "~") for part in paths[int(slot)].split("/")[1:]]
            if len(parts) == 3 and parts[0] == "memberList" and parts[1].startswith("@"):
                tag = normalize_player_tag(json.loads(parts[1][1:]).get("tag"))
                if tag is None:
                    raise ImportMismatch("invalid_roster_tag", checks)
                roster.setdefault(tag, {})[parts[2]] = value[0]
        if any(not isinstance(row.get("name"), str) or not row["name"]
               or row.get("role") not in ("member", "admin", "coLeader", "leader")
               or type(row.get("townHallLevel")) is not int or row["townHallLevel"] <= 0 for row in roster.values()):
            raise BootstrapRequired("incomplete_roster_fields:" + clan.code, checks)
        current = list(connection.execute(
            "SELECT a.player_tag,m.clan_role,m.last_observed_at FROM clan_membership m "
            "JOIN accounts a ON a.id=m.account_id JOIN clans c ON c.id=m.clan_id "
            "WHERE c.tag=? AND m.to_at IS NULL", (clan.tag,)))
        tags = {row["player_tag"] for row in current}
        result[clan.code] = {"source": len(roster), "record": len(current), "declared": item["returned_count"],
                             "observed_at": item["fetched_at"]}
        checks["clans"] = result
        if tags != set(roster) or len(current) != len(tags) or len(tags) != item["returned_count"]:
            raise ImportMismatch("roster_set_or_count_mismatch:" + clan.code, checks)
        if any(row["clan_role"] != roster[row["player_tag"]]["role"]
               or row["last_observed_at"] != item["fetched_at"] for row in current):
            raise ImportMismatch("roster_role_or_time_mismatch:" + clan.code, checks)
        if seen & tags:
            raise ImportMismatch("duplicate_current_location", checks)
        seen.update(tags)
    if len(runs) != 1:
        raise BootstrapRequired("inconsistent_family_generation", checks)
    checks["roster_accounts"] = len(seen)
    checks["outside_family_links"] = len(set(checks.pop("linked_tags")) - seen)
    return checks


def import_people(connection, path, frozen=None):
    """Caller holds the family writer; relevant legacy writers must be stopped."""
    frozen = snapshot(path) if frozen is None else frozen
    marker = connection.execute("SELECT * FROM imports WHERE topic=?", (TOPIC,)).fetchone()
    unchanged = marker is not None and marker["current_links_digest"] == frozen.links_digest
    with sqlite_transaction(connection, immediate=True):
        if not unchanged:
            item = receipt(connection, "people_membership_import", TOPIC, frozen.frozen_at, len(frozen.links))
            apply_rows(connection, frozen.normalized, frozen.frozen_at, item, basis="legacy_current", reset=True, aliases=True)
        checks = check_mapping(connection, frozen.normalized, constraints=True)
        checks["suggestions"] = len(frozen.auxiliary["suggestions"])
        checks["ignored_tags"] = len(frozen.auxiliary["ignored_tags"])
        # The roster proves the first switch only. Once imported, the marker
        # and the legacy comparison are sufficient, including after rollback.
        if marker is None:
            checks["linked_tags"] = sorted(frozen.normalized)
            roster_checks(connection, frozen.frozen_at, checks)
        if snapshot(path, frozen.frozen_at).content_digest != frozen.content_digest:
            raise ImportMismatch("legacy_changed_during_import", checks)
        if not unchanged:
            connection.execute("INSERT INTO link_imports(source_version,source_digest,frozen_at,basis,checks_json,source_item_id) "
                               "VALUES (?,?,?,?,?,?)", (frozen.version, frozen.content_digest, frozen.frozen_at,
                               "rollback_gap" if marker else "initial", canonical_json(checks), item))
            connection.execute(
                "INSERT INTO imports VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(topic) DO UPDATE SET "
                "source_version=excluded.source_version,source_digest=excluded.source_digest,frozen_at=excluded.frozen_at,"
                "current_links_digest=excluded.current_links_digest,checked_at=excluded.checked_at,"
                "checks_json=excluded.checks_json,source_item_id=excluded.source_item_id",
                (TOPIC, frozen.version, frozen.content_digest, frozen.frozen_at, frozen.links_digest,
                 frozen.frozen_at, canonical_json(checks), item),
            )
        return {"imported": not unchanged, "source_version": frozen.version, "source_digest": frozen.content_digest,
                "frozen_at": frozen.frozen_at, "checks": checks}
