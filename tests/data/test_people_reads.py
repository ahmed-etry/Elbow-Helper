"""Synthetic reads, partial/outage paths and offline legacy parity."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.configuration.clans import CLANS as CONFIGURED_CLANS
from elbow_helper.data.reads import MembershipReads, PeopleReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.players import collect_player

from . import test_family_store as fixtures
from .test_family_store import CLANS, player, clan_response, discord_member, link, response
from .test_players import profile
from .people_parity import compare_frozen


class PeopleReadTests(unittest.IsolatedAsyncioTestCase):
    clans = fixtures.FamilyStoreTests.clans
    links = fixtures.FamilyStoreTests.links
    discord = fixtures.FamilyStoreTests.discord

    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.people = PeopleReads(self.store, alias_reader=lambda: [])
        self.membership = MembershipReads(self.store, clans=CLANS)

    async def observe_profile(self, tag, at, payload=None, status=200):
        account_id = await self.store.call(lambda c: c.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()[0])
        run = await self.store.begin_run("players", "accounts", [("player_profile", tag)], at)
        await self.store.call(collect_player, run, 0, account_id, tag, response(payload, status), at)
        await self.store.finish(run)

    async def test_accounts_owners_preserve_empty_members_alts_and_time_bounds(self):
        await self.links(100, {**link(), "#Q0": {**link()["#P0"], "player_tag": "#Q0"}})
        await self.links(200, {**link(200), "#Q0": {**link()["#P0"], "player_tag": "#Q0"}})
        before = await self.people.accounts([100, 200, 300], 150)
        self.assertEqual([[a["player_tag"] for a in row["accounts"]] for row in before["rows"]], [["#P0", "#Q0"], [], []])
        owners = (await self.people.owners(["pO", "#Q0"], 200))["rows"]
        self.assertEqual([row["member_id"] for row in owners], [200, 100])
        self.assertEqual(owners[0]["interval"]["previous_good_at"], 100)
        self.assertFalse(owners[0]["interval"]["exact_start"])
        self.assertIsNone((await self.people.owners(["#P0"], 50))["rows"][0]["member_id"])
        empty = await self.people.accounts([999], 50)
        self.assertFalse(empty["rows"][0]["accounts_complete"])
        self.assertEqual(empty["coverage"]["signals"][0]["status"], "not_tracked")
        page1 = await self.people.accounts([100, 200, 300], 200, limit=1)
        page2 = await self.people.accounts([100, 200, 300], 200, limit=1, offset=page1["next_offset"])
        self.assertEqual(len(page1["rows"]), 3)
        self.assertTrue(page1["coverage"]["truncated"])
        self.assertIsNone(page2["next_offset"])

    async def test_history_generations_require_every_clan_and_keep_latest_128(self):
        for index in range(130):
            await self.clans(100 + index)
        await self.clans(300, [clan_response(0, [], declared=1), clan_response(1, [])])
        result = await self.membership.generations(0, 301)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['rows']), 128)
        self.assertEqual([result['rows'][0]['at'], result['rows'][-1]['at']], [102, 229])
        self.assertFalse(any(row['at'] == 300 for row in result['rows']))

    async def test_profile_history_preserves_moves_ownership_names_and_time_bounds(self):
        await self.clans(100)
        await self.links(100, link(player_name_last_seen='Legacy name'))
        await self.clans(200, [clan_response(0, []), clan_response(1, [player(name='New name', role='admin')])])
        await self.links(200, link(200, player_name_last_seen='Legacy name'))
        old = {row['player_tag']: row for row in (await self.people.account_history(['#P0', '#G0'], 150))['rows']}
        self.assertIsNone(old['#G0']['record_history'])
        history = old['#P0']['record_history']
        self.assertEqual((history['first_observed_at'], history['last_observed_at']), (100, 100))
        self.assertFalse(history['first_observed_is_first_ever'])
        self.assertEqual([r['member_id'] for r in history['ownership_intervals']], [100])
        self.assertEqual([r['clan_tag'] for r in history['clan_intervals']], [CLANS[0].tag])
        for interval in history['ownership_intervals'] + history['clan_intervals']:
            self.assertIsNone(interval['to_at'])
            self.assertIsNone(interval['end_previous_good_at'])
        self.assertEqual(history['names'], [{'name': 'Player', 'from_at': 100, 'to_at': None}])
        current = (await self.people.account_history(['#P0'], 200))['rows'][0]['record_history']
        self.assertEqual([r['member_id'] for r in current['ownership_intervals']], [200, 100])
        self.assertEqual([r['clan_tag'] for r in current['clan_intervals']], [CLANS[1].tag, CLANS[0].tag])
        self.assertEqual(current['clan_intervals'][0]['role'], 'admin')
        self.assertEqual(current['clan_intervals'][1]['to_at'], 200)
        self.assertEqual(current['ownership_intervals'][1]['to_at'], 200)
        self.assertEqual(current['legacy_name'], 'Legacy name')

    async def test_profile_history_limits_each_section_and_reports_truncation(self):
        await self.clans(100)
        def aliases(connection):
            account_id, item = connection.execute('SELECT account_id,source_item_id FROM account_aliases').fetchone()
            connection.executemany(
                'INSERT INTO account_aliases(account_id,name,search_key,from_at,to_at,source_item_id) VALUES (?,?,?,?,?,?)',
                [(account_id, f'Alias {i}', f'alias {i}', 101 + i, 102 + i, item) for i in range(201)])
            connection.commit()
        await self.store.call(aliases)
        history = (await self.people.account_history(['#P0'], 400))['rows'][0]['record_history']
        self.assertTrue(history['truncated'])
        self.assertEqual(len(history['names']), 200)
        self.assertEqual([history['names'][0]['from_at'], history['names'][-1]['from_at']], [301, 102])

    async def test_cohort_intervals_any_all_and_partial_scan(self):
        await self.discord(100, [discord_member(), discord_member(200, roles=[20])], complete=True)
        await self.discord(200, [discord_member(roles=[30])], complete=False)
        rows = (await self.people.cohort([10, 20], 150, match="all"))["rows"]
        self.assertEqual([row["member_id"] for row in rows], [100])
        result = await self.people.cohort([20], 200)
        self.assertEqual([row["member_id"] for row in result["rows"]], [200])
        self.assertFalse(result["coverage"]["complete"])
        await self.discord(300, [discord_member(roles=[])], complete=True)
        self.assertEqual((await self.people.cohort([20], 300))["rows"], [])

    async def test_resolve_unicode_ambiguous_mentions_tags_and_historical_aliases(self):
        await self.clans(100, [clan_response(0, [player(name="Ｆｏｘ")]), clan_response(1, [player("#Q0", name="Fox")])])
        await self.discord(100, [discord_member(name="Fox", nick="The Captain")], complete=True)
        await self.links(100, link())
        result = await self.people.resolve("fox", 100, limit=1)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidate_count"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual((await self.people.resolve("<@!100>", 100))["rows"][0]["member_id"], 100)
        self.assertEqual((await self.people.resolve("100", 100))["status"], "resolved")
        self.assertEqual((await self.people.resolve("capt", 100))["rows"][0]["member_id"], 100)
        self.assertEqual((await self.people.resolve("#pO", 100))["rows"][0]["player_tag"], "#P0")
        await self.clans(200, [clan_response(0, [player(name="New name")]), clan_response(1, [])])
        self.assertEqual((await self.people.resolve("Ｆｏｘ", 200))["status"], "ambiguous")
        self.assertEqual((await self.people.account_summary(["#P0"], 100))["rows"][0]["player_name"], "Ｆｏｘ")

    async def test_alias_adapter_runs_off_loop_and_failure_is_unknown(self):
        loop_thread = threading.get_ident()
        seen = []
        def adapter():
            seen.append(threading.get_ident())
            return [{"player_tag": "#G0", "player_name": "Old Directory"}]
        people = PeopleReads(self.store, alias_reader=adapter)
        result = await people.resolve("Old Directory", 100)
        self.assertEqual(result["rows"][0]["player_tag"], "#G0")
        self.assertIsNone(result["rows"][0]["ownership"]["member_id"])
        self.assertNotIn(loop_thread, seen)
        def broken():
            raise sqlite3.OperationalError("synthetic outage")
        result = await PeopleReads(self.store, alias_reader=broken).resolve("Absent", 100)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["missing_sources"][0]["cause"], "unavailable")

    async def test_partial_generation_outage_and_transfer_keep_original_good_time(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, [], declared=1), clan_response(1, [])])
        result = await self.membership.current(tags=["#P0"], as_of=2_000_000_100)
        row = result["rows"][0]
        self.assertEqual((row["clan_tag"], row["status"]), ("#PY", "last_known_clan"))
        self.assertEqual(row["coverage"]["signals"][0]["last_good_at"], 100)
        self.assertEqual(row["coverage"]["signals"][0]["status"], "stale")
        self.assertFalse(result["family_generation_complete"])
        self.assertTrue(row["coverage"]["signals"][0]["gaps"])
        await self.clans(2_000_000_200, [response(None, 503), clan_response(1, [player()])])
        result = await self.membership.at(2_000_000_200, tags=["#P0"])
        self.assertEqual(result["rows"][0]["clan_tag"], "#LQ")
        history = await self.membership.history(0, 2_000_000_300, tags=["#P0"])
        self.assertEqual([s["start_kind"] for s in history["rows"]], ["first_observed", "transfer"])
        self.assertEqual(history["rows"][1]["previous_good_at"], 200)
        self.assertFalse(history["coverage"]["complete"])

    async def test_membership_history_merges_roles_returns_and_half_open_windows(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, [player(role="admin")]), clan_response(1, [])])
        await self.clans(300, [clan_response(0, []), clan_response(1, [])])
        await self.clans(400)
        result = await self.membership.history(0, 500, tags=["#P0"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual([r["role"] for r in result["rows"][0]["roles"]], ["member", "admin"])
        self.assertEqual(result["rows"][1]["start_kind"], "return")
        self.assertEqual(result["rows"][0]["events"][-1]["last_confirmed_at"], 200)
        self.assertEqual((await self.membership.at(200, tags=["#P0"]))["rows"][0]["role"], "admin")
        self.assertEqual((await self.membership.at(300, tags=["#P0"]))["rows"][0]["status"], "unknown_destination")
        page1 = await self.membership.history(0, 500, limit=1)
        self.assertTrue(page1["truncated"])
        self.assertEqual(len((await self.membership.history(0, 500, offset=1, limit=1))["rows"]), 1)
        self.assertFalse(result["rows"][0]["first_observed_is_first_ever"])

    async def test_summary_clanless_unlinked_partial_profile_and_per_account_outage(self):
        await self.links(100, {**link(), "#Q0": {**link()["#P0"], "player_tag": "#Q0"}})
        payload = profile()
        payload.pop("clan")
        await self.observe_profile("#P0", 200, payload)
        await self.observe_profile("#Q0", 200, status=503)
        rows = (await self.people.account_summary(["#P0", "#Q0", "#G0"], 200))["rows"]
        self.assertEqual(rows[0]["coverage"]["status"], "not_tracked")  # tag sort: G0, P0, Q0
        self.assertEqual(rows[1]["townHallLevel"], payload["townHallLevel"])
        self.assertEqual(rows[1]["fields"]["trophies"], payload["trophies"])
        self.assertEqual(rows[2]["coverage"]["status"], "unavailable")
        self.assertEqual((await self.membership.current(tags=["#P0"], as_of=200))["rows"][0]["status"], "no_clan_observed")
        await self.clans(300)
        owner = (await self.people.owners(["#G0"], 300))["rows"][0]
        self.assertIsNone(owner["member_id"])
        await self.observe_profile("#P0", 3_600_000_200, status=503)
        rows = (await self.people.account_summary(["#P0"], 8_000_000_200))["rows"]
        self.assertEqual(rows[0]["coverage"]["status"], "stale")
        self.assertEqual(rows[0]["coverage"]["last_good_at"], 200)
        self.assertEqual(rows[0]["townHallLevel"], 16)  # newer validated roster

    async def test_one_query_only_transaction_per_batch_and_disk_failure(self):
        await self.links(100, link())
        traces = []
        original = self.store._call
        def traced(operation, args, kwargs):
            def wrapper(c, *args, **kwargs):
                c.set_trace_callback(traces.append)
                return operation(c, *args, **kwargs)
            return original(wrapper, args, kwargs)
        with patch.object(self.store, "_call", traced):
            await self.people.accounts([100, 200], 100)
        self.assertEqual(sum(s == "BEGIN" for s in traces), 1)
        self.assertEqual(sum(s == "COMMIT" for s in traces), 1)
        self.assertEqual(sum("SELECT * FROM collection_items WHERE source_kind='account_links'" in s for s in traces), 1)
        with self.assertRaises(sqlite3.OperationalError):
            await self.people._read(lambda c: c.execute("DELETE FROM account_links"))
        self.assertEqual((await self.people.owners(["#P0"], 100))["rows"][0]["member_id"], 100)
        self.path.rename(self.path.with_suffix(".missing"))
        with self.assertRaises(FileNotFoundError):
            await self.people.accounts([100], 100)

    async def test_history_cannot_borrow_future_observations_or_endpoints(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, []), clan_response(1, [player()])])
        at = (await self.membership.at(150, tags=["#P0"]))["rows"][0]
        self.assertIsNone(at["stay"]["to_at"])
        self.assertEqual(at["stay"]["last_confirmed_at"], 100)
        result = await self.membership.history(100, 200)
        self.assertEqual(len(result["rows"]), 1)
        self.assertIsNone(result["rows"][0]["to_at"])
        self.assertEqual(result["rows"][0]["coverage"]["last_good_at"], 100)
        result = await self.membership.history(200, 201)
        self.assertEqual(len(result["rows"]), 2)  # absence at the inclusive start

    async def test_ownership_outage_preserves_links_and_unknown_age_metadata(self):
        await self.links(100, link(player_name_last_seen="Legacy Name", last_seen_clan_tag="#PY"))
        run = await self.store.begin_run("links", "current_links", [("account_links", "current")], 200)
        await self.store.call(lambda c: FamilyStore.outcome(c, FamilyStore.item(c, run, 0)["id"], "failed", 200, complete=False))
        await self.store.finish(run)
        row = (await self.people.accounts([100], 700_000_100))["rows"][0]
        self.assertFalse(row["accounts_complete"])
        self.assertEqual(row["coverage"]["status"], "stale")
        account = row["accounts"][0]
        self.assertEqual(account["player_name"], "Legacy Name")
        self.assertIsNone(account["link_metadata"]["observed_at"])
        self.assertEqual(account["field_sources"]["name"], "legacy_unknown_age")
        location = (await self.membership.at(700_000_100, tags=["#P0"]))["rows"][0]
        self.assertEqual(location["status"], "unknown_location")
        self.assertIsNone(location["clan_tag"])

    async def test_partial_profile_scalar_presence_missing_counters_and_compact_spans(self):
        await self.links(100, link())
        payload = profile()
        payload.pop("troops")
        payload.pop("warStars")
        payload["trophies"] = 0
        await self.observe_profile("#P0", 200, payload)
        row = (await self.people.account_summary(["#P0"], 200))["rows"][0]
        self.assertTrue(row["coverage"]["field_completeness"]["fields"])
        self.assertFalse(row["coverage"]["field_completeness"]["arrays"]["troops"])
        self.assertFalse(row["field_presence"]["warStars"])
        self.assertNotIn("warStars", row["fields"])
        self.assertEqual(row["fields"]["trophies"], 0)
        await self.observe_profile("#P0", 3_600_000_200, payload)
        await self.observe_profile("#P0", 7_200_000_200, payload)
        coverage = (await self.people.account_summary(["#P0"], 5_400_000_200))["rows"][0]["coverage"]
        self.assertTrue(coverage["compact_spans"])
        self.assertLessEqual(coverage["last_good_at"], 5_400_000_200)
        self.assertEqual(coverage["status"], "partial")
        self.assertFalse(coverage["continuous_state_known"])

    async def test_complete_generation_empty_rosters_conflicts_and_paging(self):
        await self.clans(100, [clan_response(0, [player(), player("#Q0")]), clan_response(1, [])])
        result = await self.membership.current(as_of=100, limit=1)
        self.assertTrue(result["family_generation_complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual((await self.membership.current(as_of=100, offset=1, limit=1))["rows"][0]["player_tag"], "#Q0")
        self.assertIsNone(result["rows"][0]["ownership"]["member_id"])
        await self.clans(200, [clan_response(0, [player()]), clan_response(1, [player()])])
        result = await self.membership.current(as_of=200)
        self.assertFalse(result["family_generation_complete"])
        self.assertEqual(result["rows"][0]["status"], "last_known_clan")
        await self.clans(300, [clan_response(0, []), clan_response(1, [])])
        result = await self.membership.current(as_of=300)
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["family_generation_complete"])

    async def test_newer_outside_profile_preserves_conflicting_roster_evidence(self):
        await self.clans(100)
        payload = profile()
        payload["clan"] = {"tag": "#GJ", "name": "Outside clan"}
        await self.observe_profile("#P0", 200, payload)
        row = (await self.membership.current(tags=["#P0"], as_of=200))["rows"][0]
        self.assertEqual(row["status"], "conflicting_observations")
        self.assertEqual(row["profile_location"]["clan"]["tag"], "#GJ")
        self.assertEqual(row["clan_tag"], "#PY")
        self.assertEqual(row["stay"]["last_confirmed_at"], 100)

    async def test_role_cohort_keeps_configured_home_and_utility_assignments_distinct(self):
        clans = [SimpleNamespace(code="HOME", tag="#PY", member_role_id=10, is_utility=False),
                 SimpleNamespace(code="EXTRA", tag="#LQ", member_role_id=20, is_utility=True)]
        people = PeopleReads(self.store, clans=clans)
        await self.discord(100, [discord_member()], complete=True)
        row = (await people.cohort([10, 20], 100, match="all"))["rows"][0]
        self.assertEqual([c["clan_code"] for c in row["home_clans"]], ["HOME"])
        self.assertEqual([c["clan_code"] for c in row["participation_clans"]], ["EXTRA"])

    async def test_alias_and_observation_evidence_is_bounded_with_explicit_truncation(self):
        aliases = [{"player_tag": "#P0", "player_name": f"Alias {index}"} for index in range(205)]
        people = PeopleReads(self.store, alias_reader=lambda: aliases)
        result = await people.resolve("#P0", 100)
        self.assertEqual(len(result["rows"][0]["aliases"]), 200)
        self.assertTrue(result["rows"][0]["aliases_truncated"])
        self.assertTrue(result["coverage"]["truncated"])

    async def test_invalid_arguments_and_unknown_sources(self):
        for call in (self.people.accounts([0]), self.people.accounts(range(1, 202)),
                     self.people.owners(["invalid"]), self.people.cohort([1], match="bad"),
                     self.people.resolve(""), self.people.resolve("x" * 101),
                     self.people.accounts([1], limit=201), self.membership.history(2, 1),
                     self.membership.at(-1), self.membership.current(clans=["bad"])):
            with self.assertRaises(ValueError):
                await call
        result = await self.membership.at(100, tags=["#P0"])
        self.assertEqual(result["rows"][0]["status"], "unknown_location")
        self.assertFalse(result["coverage"]["complete"])


    async def test_frozen_facade_and_agent_parity_lists_every_difference(self):
        clans = tuple(CONFIGURED_CLANS.values())
        payloads = [profile(tag) for tag in ("#P0", "#Q0", "#G0")]
        for payload, name in zip(payloads, ("Alpha", "Clanless Alt", "Unlinked")):
            payload.update(name=name, townHallLevel=16)
            payload["clan"] = {"tag": clans[0].tag, "name": clans[0].name}
        payloads[1].pop("clan")
        rosters = {clan.tag: {"tag": clan.tag, "name": clan.name, "members": 0, "memberList": []} for clan in clans}
        rosters[clans[0].tag].update(members=2, memberList=[
            player("#P0", name="Alpha"), player("#G0", name="Unlinked")])
        from elbow_helper.data.store.clans import collect_clans
        specs = [(kind, clan.tag) for clan in clans for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, 100)
        await self.store.call(collect_clans, run, clans, [response(rosters[c.tag]) for c in clans], 100)
        await self.store.finish(run)
        await self.links(100, {**link(player_name_last_seen="Alpha"), "#Q0": {
            **link(player_name_last_seen="Clanless Alt")["#P0"], "player_tag": "#Q0"}})
        held_role = clans[0].member_role_id
        await self.discord(100, [discord_member(roles=[held_role]), discord_member(200, name="No Links", roles=[])], complete=True)
        for payload in payloads:
            await self.observe_profile(payload["tag"], 100, payload)
        directory = [{"player_tag": "#P0", "player_name": "Old Alpha"}]
        people = PeopleReads(self.store, alias_reader=lambda: deepcopy(directory))
        membership = MembershipReads(self.store)
        links_path = self.path.parent / "frozen-links.sqlite3"
        def create_links():
            with closing(sqlite3.connect(links_path)) as c, c:
                c.execute("CREATE TABLE links(player_tag TEXT PRIMARY KEY,discord_user_id INTEGER,player_name_last_seen TEXT,"
                          "last_seen_clan_tag TEXT,last_seen_clan_code TEXT,last_seen_role TEXT)")
                c.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)", [
                    ("#P0", 100, "Alpha", "", None, "member"), ("#Q0", 100, "Clanless Alt", "", None, "member")])
        await self.store.call(lambda c: create_links())
        role = SimpleNamespace(id=held_role, name="Home", is_default=lambda: False)
        members = [SimpleNamespace(id=identifier, name=name, display_name="Display", nick="Nickname", bot=False,
                    roles=[role] if identifier == 100 else [], joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                   for identifier, name in ((100, "Username"), (200, "No Links"))]
        frozen = dict(as_of=100, members=members, profiles={p["tag"]: p for p in payloads}, rosters=rosters,
                      member_ids=[100, 200, 300], queries=["Alpha", "Old Alpha", "Unlinked"], directory=directory)
        differences = await compare_frozen(people, membership, links_path, **frozen)
        self.assertEqual([d for d in differences if d["classification"] == "bug"], [])
        self.assertEqual([d["path"] for d in differences], ["history"])
        # A changed frozen baseline is reported field by field; the harness
        # must not silently approve differences just because counts agree.
        frozen["profiles"]["#P0"] = {**payloads[0], "townHallLevel": 17, "name": "Different", "role": "admin"}
        differences = await compare_frozen(people, membership, links_path, **frozen)
        bugs = {d["path"] for d in differences if d["classification"] == "bug"}
        self.assertTrue({"agent/accounts/#P0/townHallLevel", "agent/accounts/#P0/player_name", "agent/accounts/#P0/role"} <= bugs)
