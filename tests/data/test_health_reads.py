"""Clan health's record reads: progress, donations, raid weekends, clan stays and the account directory (synthetic)."""

import json
import unittest

from elbow_helper.data.reads import ActivityReads, RaidReads, StayReads
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_raid_lines import CLAN, FIRST, SECOND as SECOND_WEEKEND, STARTS, RaidFixture, body, member, weekend

US = 1_000_000
HOUR = 3600 * US
DAY = 24 * HOUR
T = 1_790_000_000 * US
OTHER = "#PP8"
BITS = {"warStars": 1 << 4, "attackWins": 1 << 5, "clanCapitalContributions": 1 << 7,
        "donations": 1 << 8, "donationsReceived": 1 << 9}


class RecordFixture(RaidFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.activity, self.stays = ActivityReads(self.store), StayReads(self.store)
        self.players = await self.sql("INSERT INTO collection_runs(collector,scope_kind,scheduled_at,started_at,status,"
                                      "expected) VALUES ('players','synthetic',1,1,'ok',0)")
        self.legacy = await self.sql("INSERT INTO collection_runs(collector,scope_kind,scheduled_at,started_at,status,"
                                     "expected) VALUES ('profile_import','synthetic',1,1,'ok',0)")
        self.item = await self.sql("INSERT INTO collection_items(run_id,ordinal,source_kind,status,started_at) "
                                   "VALUES (?,0,'synthetic','ok',1)", (self.players,))
        self.clans = {}
        for tag, code in ((CLAN, "ONE"), (OTHER, "TWO")):
            self.clans[tag] = await self.sql("INSERT INTO clans(tag,family_code,name,is_family,wiring_revision) "
                                             "VALUES (?,?,'Synthetic clan',1,1)", (tag, code))

    async def sql(self, statement, args=()):
        def run(connection):
            with sqlite_transaction(connection):
                return connection.execute(statement, args).lastrowid
        return await self.store.call(run)

    async def account(self, tag, name=None):
        identifier = await self.sql("INSERT INTO accounts(player_tag,current_name,first_observed_at,tracking_status) "
                                    "VALUES (?,?,1,'observed')", (tag, name))
        return identifier

    async def counter(self, account, at, run=None, achievements=None, **values):
        columns = {"warStars": "war_stars", "attackWins": "attack_wins",
                   "clanCapitalContributions": "capital_contributions", "donations": "donations",
                   "donationsReceived": "received"}
        presence = sum(BITS[key] for key in values)
        await self.sql(
            f"INSERT INTO player_counters(account_id,sampled_at,{','.join(columns[k] for k in values) or 'trophies'},"
            "achievement_values_json,transient_item_fields_json,field_presence_json,reset_context_json,source_run_id) "
            f"VALUES (?,?,{','.join('?' for _ in values) or 'NULL'},?,'{{}}',?,'{{}}',?)",
            (account, at, *values.values(), json.dumps(achievements or {}), json.dumps({"fields": presence}),
             run or self.players))

    async def catalogue(self, source, village, name, identity=""):
        return await self.sql("INSERT INTO item_catalog(source_array,village,source_name,definition_json,identity_key) "
                              "VALUES (?,?,?,'{}',?)", (source, village, name, identity))

    async def level(self, account, item, at, level, present=1):
        await self.sql("INSERT INTO player_items VALUES (?,?,?,?,NULL,'{}',?,NULL,?)",
                       (account, item, at, level, present, self.players))

    async def stay(self, account, clan, from_at, to_at=None, last=None, end_previous=None):
        await self.sql("INSERT INTO clan_membership(account_id,clan_id,clan_role,from_at,to_at,last_observed_at,"
                       "end_previous_good_at,end_basis,source_item_id) VALUES (?,?,'member',?,?,?,?,?,?)",
                       (account, self.clans[clan], from_at, to_at, last or from_at, end_previous,
                        None if to_at is None else "observed_absence", self.item))

    async def old_stay(self, account, clan, from_at, to_at, end_basis="last_capture"):
        await self.sql("INSERT INTO old_clan_stays VALUES (?,?,?,?,?,1,?,'observed_hourly','legacy_health',?)",
                       (account, clan, from_at, to_at, to_at, end_basis, self.legacy))


class ProgressTests(RecordFixture):

    async def test_progress_at_a_moment_from_items_counters_and_achievements(self):
        account = await self.account("#PP29L")
        king = await self.catalogue("heroes", "home", "Barbarian King")
        owl = await self.catalogue("troops", "home", "Electro Owl")
        gone = await self.catalogue("spells", "home", "Rage Spell")
        early = await self.catalogue("achievements", "home", "Games Champion", "Earn 10000 points in Clan Games")
        later = await self.catalogue("achievements", "home", "Games Champion", "Earn 50000 points in Clan Games")
        await self.level(account, king, T, 80)
        await self.level(account, owl, T, 7)
        await self.level(account, gone, T, 5)
        await self.level(account, king, T + DAY, 82)
        await self.level(account, gone, T + DAY, None, present=0)
        await self.counter(account, T, achievements={str(early): 9000}, clanCapitalContributions=100, warStars=5)
        # The achievement moved to its next tier: the old item drops out, the new one carries the value.
        await self.counter(account, T + DAY, achievements={str(early): None, str(later): 12000}, warStars=7)
        await self.sql("INSERT INTO player_observations VALUES (?,?,?,?,NULL,?)",
                       (account, "townHallLevel", T, "16", self.players))
        await self.sql("INSERT INTO donation_snapshots(account_id,sampled_at,trophies,field_presence_json,source_item_id) "
                       "VALUES (?,?,5100,'{}',?)", (account, T + HOUR, self.item))
        first = (await self.activity.progress(["#PP29L"], as_of=T + HOUR))["rows"][0]
        self.assertEqual({key: first[key] for key in ("townhall", "trophies", "hero_sum", "pet_sum", "spell_sum",
                                                      "games_total", "capital_contrib", "war_stars")},
                         {"townhall": 16, "trophies": 5100, "hero_sum": 80, "pet_sum": 7, "spell_sum": 5,
                          "games_total": 9000, "capital_contrib": 100, "war_stars": 5})
        second = (await self.activity.progress(["#PP29L"], as_of=T + DAY))["rows"][0]
        self.assertEqual((second["hero_sum"], second["spell_sum"], second["games_total"], second["war_stars"],
                          second["capital_contrib"]), (82, 0, 12000, 7, 100))
        self.assertEqual(second["sources"]["games_total"], "record")

    async def test_old_totals_stand_before_the_record_and_the_first_values_fill_a_late_start(self):
        account = await self.account("#PP29L")
        await self.sql("INSERT INTO old_profile_totals VALUES (?,?,50,3,10,200,60,8000,'legacy_health',?)",
                       (account, T - DAY, self.legacy))
        king = await self.catalogue("heroes", "home", "Barbarian King")
        await self.level(account, king, T + DAY, 70)
        before = (await self.activity.progress(["#PP29L"], as_of=T))["rows"][0]
        self.assertEqual((before["hero_sum"], before["games_total"], before["sources"]["hero_sum"]),
                         (50, 8000, "legacy_health"))
        after = (await self.activity.progress(["#PP29L"], as_of=T + DAY))["rows"][0]
        self.assertEqual((after["hero_sum"], after["games_total"]), (70, 8000))
        # A later record value never replaces an old total the moment already has.
        window = (await self.activity.progress(["#PP29L"], as_of=T, until=T + 2 * DAY))["rows"][0]
        self.assertEqual((window["hero_sum"], window["sources"]["hero_sum"]), (50, "legacy_health"))
        # With neither at the moment, the first inside the window stands, whichever source saw it.
        late_old = await self.account("#PP29G")
        await self.sql("INSERT INTO old_profile_totals VALUES (?,?,40,NULL,NULL,NULL,NULL,NULL,'legacy_health',?)",
                       (late_old, T + HOUR, self.legacy))
        await self.level(late_old, king, T + DAY, 45)
        first = (await self.activity.progress(["#PP29G"], as_of=T, until=T + 2 * DAY))["rows"][0]
        self.assertEqual((first["hero_sum"], first["observed_at"]["hero_sum"]), (40, T + HOUR))
        # Nothing at or before the moment: the first values up to ``until`` stand in.
        newcomer = await self.account("#PP29Q")
        await self.counter(newcomer, T + 2 * HOUR, warStars=4)
        late = (await self.activity.progress(["#PP29Q"], as_of=T, until=T + DAY))["rows"][0]
        self.assertEqual((late["war_stars"], late["observed_at"]["war_stars"]), (4, T + 2 * HOUR))
        self.assertIsNone((await self.activity.progress(["#PP29Q"], as_of=T))["rows"][0]["war_stars"])
        unknown = (await self.activity.progress(["#PP9"], as_of=T))["rows"][0]
        self.assertFalse(unknown["known"])
        with self.assertRaises(ValueError):
            await self.activity.progress(["#PP29Q"], as_of=T, until=T - 1)


class DonationTests(RecordFixture):
    async def snapshot(self, account, at, given, received):
        await self.sql("INSERT INTO donation_snapshots(account_id,sampled_at,donations,received,field_presence_json,"
                       "source_item_id) VALUES (?,?,?,?,'3',?)", (account, at, given, received, self.item))

    async def test_window_adds_rises_counts_a_reset_in_full_and_uses_counters_before_roster_samples(self):
        account = await self.account("#PP29L")
        await self.counter(account, T - HOUR, donations=100, donationsReceived=40)
        await self.counter(account, T + HOUR, donations=130, donationsReceived=50)
        # Profile counters after the first roster sample are not read again.
        await self.counter(account, T + 5 * HOUR, donations=999, donationsReceived=999)
        await self.snapshot(account, T + 2 * HOUR, 150, 55)
        await self.snapshot(account, T + 3 * HOUR, 20, 5)
        await self.snapshot(account, T + 4 * HOUR, 25, 9)
        await self.snapshot(account, T + 10 * HOUR, 90, 90)
        result = (await self.activity.donations(["#PP29L", "#PP29Q"], after=T, before=T + 10 * HOUR))["rows"]
        # 100 -> 130 -> 150 adds 50; the reset to 20 counts 20; then 5 more.
        self.assertEqual(result[0], {"player_tag": "#PP29L", "donations": 75, "donations_received": 24, "samples": 4})
        self.assertEqual(result[1], {"player_tag": "#PP29Q", "donations": 0, "donations_received": 0, "samples": 0})
        with self.assertRaises(ValueError):
            await self.activity.donations(["#PP29L"], after=T, before=T)


class RaidWeekendTests(RecordFixture):
    async def test_weekends_that_ended_in_the_window_with_old_only_weekends_and_lines(self):
        await self.fetch(body(weekend(FIRST, [member("#PP29L", 6, 900), member("#PP29Q", 0, 0)]),
                              weekend(SECOND_WEEKEND, [])))
        older = STARTS[FIRST] - 7 * DAY
        await self.sql("INSERT INTO old_raid_weekends VALUES (?,?,?,1,'legacy_health',?)",
                       (OTHER, older, older + 3 * DAY, self.legacy))
        await self.sql("INSERT INTO raid_member_lines VALUES (?,?,'#PP29L',NULL,4,5,1,300,?,?)",
                       (OTHER, older, older + 3 * DAY, self.legacy))
        reads = RaidReads(self.store)
        result = await reads.ended(after=older + 3 * DAY, before=STARTS[FIRST] + 3 * DAY + 1)
        self.assertEqual([(w["clan_tag"], w["origin"], len(w["members"])) for w in result["weekends"]],
                         [(OTHER, "legacy_health", 1), (CLAN, "record", 2)])
        self.assertEqual(result["weekend_ends"], [older + 3 * DAY, STARTS[FIRST] + 3 * DAY])
        # A weekend that ends after the window, and one without lines, are left out.
        cut = await reads.ended(after=older, before=STARTS[FIRST] + 3 * DAY, player_tags=["#PP29L"])
        self.assertEqual([(w["clan_tag"], [m["player_tag"] for m in w["members"]]) for w in cut["weekends"]],
                         [(OTHER, ["#PP29L"])])
        selected = await reads.ended(after=older, before=STARTS[FIRST] + 4 * DAY, player_tags=["#PP29Q"])
        self.assertEqual(selected["weekend_ends"], [older + 3 * DAY, STARTS[FIRST] + 3 * DAY])
        self.assertEqual([len(w["members"]) for w in selected["weekends"]], [0, 1])


class StayTests(RecordFixture):
    async def test_stays_join_copied_and_record_stays_and_rosters_follow_the_last_capture(self):
        old_member = await self.account("#PP29L", "Old member")
        mover = await self.account("#PP29Q", "Mover")
        await self.sql("INSERT INTO account_aliases VALUES (?,?,?,?,NULL,?,NULL)",
                       (mover, "Mover", "mover", T - 10 * DAY, self.item))
        for at in (T - 5 * DAY, T - 2 * DAY, T):
            await self.counter(old_member, at, run=self.legacy, warStars=1)
        await self.old_stay(old_member, CLAN, T - 5 * DAY, T - 2 * DAY)
        await self.old_stay(mover, OTHER, T - 5 * DAY, T, end_basis="record_membership")
        await self.stay(mover, CLAN, T, T + DAY, last=T + DAY - HOUR, end_previous=T + DAY - HOUR)
        await self.stay(mover, OTHER, T + DAY, last=T + 2 * DAY)
        # An open stay is only as recent as the clan's last good roster.
        await self.sql("INSERT INTO collection_items(run_id,ordinal,source_kind,scope_id,status,started_at,fetched_at) "
                       "VALUES (?,1,'clan_roster',?,'ok',?,?)", (self.players, OTHER, T + 2 * DAY, T + 2 * DAY))
        stays = (await self.stays.stays(["#PP29Q"], after=T - DAY, before=T + 3 * DAY))["rows"][0]["stays"]
        self.assertEqual([(s["clan_code"], s["basis"], s["from_at"], s["last_seen_at"], s["to_at"]) for s in stays],
                         [("TWO", "observed_hourly", T - 5 * DAY, T, T), ("ONE", "record", T, T + DAY - HOUR, T + DAY),
                          ("TWO", "record", T + DAY, T + 2 * DAY, None)])
        # Between captures the old roster is the last capture's: the stay that ended there still counts.
        roster = await self.stays.roster([CLAN, OTHER], at=T - 2 * DAY + HOUR)
        self.assertEqual({c["clan_tag"]: [m["player_tag"] for m in c["members"]] for c in roster["clans"]},
                         {CLAN: ["#PP29L"], OTHER: ["#PP29Q"]})
        self.assertEqual(roster["clans"][0]["observed_at"], T - 2 * DAY)
        later = await self.stays.roster([CLAN, OTHER], at=T + DAY + HOUR)
        self.assertEqual({c["clan_tag"]: [(m["player_tag"], m["basis"]) for m in c["members"]] for c in later["clans"]},
                         {CLAN: [], OTHER: [("#PP29Q", "record")]})

    async def test_directory_lookup_ranks_like_the_old_search_and_names_former_members(self):
        first = await self.account("#PP29L", "Silver Kite")
        second = await self.account("#PP29Q", "Silver")
        await self.account("#PP9", "Outside")
        await self.old_stay(first, CLAN, T - 5 * DAY, T - 4 * DAY)
        await self.old_stay(second, OTHER, T - 5 * DAY, T - 3 * DAY)
        await self.sql("INSERT INTO player_observations VALUES (?,'townHallLevel',?,'15',NULL,?)",
                       (first, T - 5 * DAY, self.players))
        rows = (await self.stays.lookup("silver", as_of=T))["rows"]
        self.assertEqual([(r["player_tag"], r["clan_code"]) for r in rows], [("#PP29Q", "TWO"), ("#PP29L", "ONE")])
        self.assertEqual([r["player_tag"] for r in (await self.stays.lookup("pp29l", as_of=T))["rows"]], ["#PP29L"])
        self.assertEqual([r["player_tag"] for r in (await self.stays.lookup("", as_of=T))["rows"]], ["#PP29Q", "#PP29L"])
        self.assertEqual([r["player_tag"] for r in (await self.stays.lookup("", clan_code="ONE", as_of=T))["rows"]],
                         ["#PP29L"])
        self.assertEqual((await self.stays.accounts(["#PP29L", "#PP9"], as_of=T))["rows"][0]["townhall"], 15)
        self.assertEqual(len((await self.stays.accounts(["#PP29L", "#PP9"], as_of=T))["rows"]), 1)
        self.assertEqual((await self.stays.names(as_of=T))["rows"], [
            {"player_tag": "#PP29L", "player_name": "Silver Kite"}, {"player_tag": "#PP29Q", "player_name": "Silver"}])

    async def test_movements_cover_the_window_and_stop_at_their_bound(self):
        accounts = [await self.account(f"#PP{index}L", f"Synthetic {index}") for index in (2, 8, 9)]
        for offset, account in enumerate(accounts):
            await self.old_stay(account, CLAN, T + offset * HOUR, T + DAY)
        moved = await self.stays.movements(after=T, before=T + 2 * DAY)
        self.assertEqual((len(moved["rows"]), moved["truncated"]), (3, False))
        self.assertEqual(moved["rows"][0]["stays"][0]["player_name"], "Synthetic 2")
        self.assertTrue((await self.stays.movements(after=T, before=T + 2 * DAY, limit=2))["truncated"])
        self.assertEqual((await self.stays.movements(after=T + 3 * DAY, before=T + 4 * DAY))["rows"], [])


if __name__ == "__main__":
    unittest.main()
