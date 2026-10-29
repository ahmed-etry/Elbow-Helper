"""Offline parity rehearsal against the actual legacy facade and agent reads.

Inputs are a frozen links file, a family record, and frozen Discord/Clash
responses. No collector, provider, model, or network client is started here.
Every differing field is returned with its old/new value and classification.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from elbow_helper.agent.families import account_names, identity_reads
from .link_fixtures import FrozenLegacyLinks as FrozenLinks
from elbow_helper.features.account_links.cog import AccountLinks


async def compare_frozen(people, membership, links_path, *, as_of, members, profiles, rosters,
                         member_ids, queries=(), directory=()):
    """Compare all tags/owners, per-member lists, roster/name/TH/role and names.

    Call with immutable snapshots made at the same freeze time. Pagination is
    exhausted locally. A difference is a new-read bug unless explicitly
    covered by a documented historical/coverage difference below.
    """
    links = FrozenLinks(links_path)
    by_member = {member.id: member for member in members}
    context = SimpleNamespace(account_links=links,
        guild=SimpleNamespace(members=members, chunked=True, get_member=by_member.get),
        cwl_queries=SimpleNamespace(account_names=lambda: deepcopy(directory)),
        state=SimpleNamespace(resolved_members={}, candidate_member_ids=set()))
    differences = []

    def compare(path, old, new, *, intended=None):
        if old != new:
            differences.append({"path": path, "old": old, "new": new,
                                "classification": "intended" if intended else "bug", "reason": intended or "new read differs"})

    async def clash(_context, arguments):
        source = profiles if arguments["kind"] == "player" else rosters if arguments["kind"] == "clan" else None
        if source is None:
            raise AssertionError("Unexpected offline source")
        return {"items": [{"tag": tag, "status": "ok" if tag in source else "unavailable",
                           "data": deepcopy(source.get(tag, {})), "observed_at": as_of}
                          for tag in arguments["tags"]]}

    old_links = await asyncio.to_thread(links.get_all_links)
    old_members = await asyncio.to_thread(links.get_links_for_members, member_ids)
    def record_tags(connection):
        return [row[0] for row in connection.execute("SELECT DISTINCT a.player_tag FROM account_links l "
                "JOIN accounts a ON a.id=l.account_id WHERE l.from_at<=? AND (l.to_at IS NULL OR l.to_at>?)", (as_of, as_of))]
    record_link_tags = await people._read(record_tags)
    all_tags = sorted(set(old_links) | set(profiles) | set(record_link_tags) |
                      {row["tag"] for clan in rosters.values() for row in clan["memberList"]})
    new_owners, summaries = {}, {}
    for start in range(0, len(all_tags), 200):
        batch = all_tags[start:start + 200]
        new_owners.update({r["player_tag"]: r for r in (await people.owners(batch, as_of))["rows"]})
        summaries.update({r["player_tag"]: r for r in (await people.account_summary(batch, as_of))["rows"]})
    for tag in all_tags:
        compare(f"owners/{tag}", old_links.get(tag, {}).get("discord_user_id"), new_owners[tag]["member_id"])
    # Include unexpected record-only links: parity is equality in both directions.
    compare("linked_tag_set", sorted(old_links), sorted(record_link_tags))
    new_members = {identifier: [] for identifier in member_ids}
    for start in range(0, len(member_ids), 200):
        offset = 0
        while True:
            result = await people.accounts(member_ids[start:start + 200], as_of, offset=offset)
            for row in result["rows"]:
                new_members[row["member_id"]].extend(a["player_tag"] for a in row["accounts"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
    for identifier in member_ids:
        compare(f"members/{identifier}/accounts", sorted(r["player_tag"] for r in old_members[identifier]),
                sorted(new_members[identifier]))
    offset, locations = 0, {}
    while True:
        result = await membership.current(as_of=as_of, limit=200, offset=offset)
        locations.update({r["player_tag"]: r for r in result["rows"]})
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    async def clan_payload(path, **kwargs):
        return deepcopy(rosters.get(unquote(path.rsplit("/", 1)[-1])))
    facade = SimpleNamespace(clash_client=SimpleNamespace(configured=True), _fetch_coc_json=clan_payload,
                             _clan_badge_urls={})
    legacy_locations = {}
    for clan in membership.clans:
        old_rows = await AccountLinks._fetch_clan_members(facade, clan.code)
        if old_rows is not None:
            legacy_locations.update({row["player_tag"]: row for row in old_rows})
    facade._player_locations = legacy_locations
    facade._last_snapshot_complete = True
    facade._location_snapshot_observed_at = as_of
    legacy_snapshot = AccountLinks.get_player_locations_snapshot(facade, tuple(legacy_locations))
    for clan in membership.clans:
        old = {tag: row for tag, row in legacy_snapshot["locations"].items() if row["clan_tag"] == clan.tag}
        new = {tag: row for tag, row in locations.items() if row["clan_tag"] == clan.tag}
        compare(f"rosters/{clan.code}/tags", sorted(old), sorted(new))
        for tag in sorted(old.keys() & new.keys()):
            for old_key, new_key in (("player_name", "player_name"), ("townhall", "townHallLevel"), ("role", "role")):
                compare(f"rosters/{clan.code}/{tag}/{new_key}", old[tag].get(old_key), new[tag].get(new_key))
    # Use production handlers, replacing only source access and access checks.
    # The production agent is otherwise executed unchanged, including joining
    # all alts, empty members and the Unicode account-name comparisons.
    with (patch.object(identity_reads, "read_clash", clash),
          patch.object(account_names, "read_clash", clash),
          patch.object(identity_reads, "require_evidence_access", AsyncMock()),
          patch.object(account_names, "require_evidence_access", AsyncMock())):
        old_result = await identity_reads.member_accounts(context, {"member_ids": member_ids, "include_roles": True})
        for member in old_result["members"]:
            if member["member_id"] in by_member:
                new_member = (await people.accounts([member["member_id"]], as_of))["rows"][0]
                compare(f"agent/members/{member['member_id']}/roles",
                        sorted(role["role_id"] for role in member["roles"]), new_member["roles"])
        for row in old_result["rows"]:
            tag = row.get("player_tag")
            if not tag:
                compare(f"agent/members/{row['member_id']}/empty", [], new_members[row["member_id"]])
                continue
            new = summaries[tag]
            for key, value in (("player_name", new["player_name"]), ("townHallLevel", new["townHallLevel"]),
                               ("role", new["role"]), ("ranked_league", new["ranked_league"]),
                               ("legacy_league", new["legacy_league"]), ("clan", new["clan"]),
                               ("trophies", new["fields"].get("trophies")),
                               ("donations", new["fields"].get("donations")), ("war_stars", new["fields"].get("warStars"))):
                compare(f"agent/accounts/{tag}/{key}", row.get(key), value)
        for query in queries:
            old = await account_names.resolve_account_names(context, [query])
            new = await people.resolve(query, as_of)
            old_tags = sorted(r["player_tag"] for r in old["name_resolutions"][0]["candidates"])
            new_tags = sorted(r["player_tag"] for r in new["rows"] if r["kind"] == "account")
            compare(f"names/{query}/tags", old_tags, new_tags,
                    intended="Record retains observed historical aliases" if set(old_tags) < set(new_tags) else None)
            compare(f"names/{query}/status", old["name_resolutions"][0]["status"], new["status"],
                    intended="New resolve also searches Discord identities" if any(r["kind"] == "member" for r in new["rows"]) else None)
    differences.append({"path": "history", "classification": "intended", "old": "current only",
                        "new": "observed intervals and gaps", "reason": "Legacy links and facade retain no dated ownership or stays"})
    return differences
