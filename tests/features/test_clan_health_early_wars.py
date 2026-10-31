"""Regular wars saved before the wars table must reach the agent's attendance read once."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database.migrations import deduplicate_regular_wars

END = 1773000000
DAY = 86400
CLAN, OPPONENT, CODE = "#2Q", "#9P", "TEST"


def war_id(end, opponent=OPPONENT):
    stamp = datetime.fromtimestamp(end, timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")
    return f"REG:{CLAN}:{stamp}:{opponent}"


class EarlyRegularWarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = ClanHealthRepository(Path(self.temp.name) / "health.db")
        self.repo.initialize()

    def capture(self, end, state, lineup, *, identity=None, opponent=OPPONENT):
        """lineup maps player tag to (attacks expected, attack orders)."""
        identity = identity or war_id(end, opponent)
        source = f"currentwar_api:{state}"
        self.repo.store_war_activity([
            dict(war_id=identity, war_type="REG", clan_code=CODE, clan_tag=CLAN,
                 end_ts=end if state.startswith("warEnded") else end - DAY // 2,
                 player_tag=tag, player_name="Synthetic", attacks_expected=expected,
                 attacks_used=len(orders), attack_count=len(orders), source=source)
            for tag, (expected, orders) in lineup.items()])
        self.repo.store_war_attacks([
            dict(war_id=identity, war_type="REG", clan_code=CODE, clan_tag=CLAN, end_ts=end,
                 war_state="warEnded" if state.startswith("warEnded") else "inWar",
                 player_tag=tag, player_name="Synthetic", attack_order=order,
                 defender_tag="#UV", stars=2, destruction=80.0, source=source)
            for tag, (_, orders) in lineup.items() for order in orders])
        return identity

    def stored_war(self, end, identity, *, prep=None, start=None):
        self.repo.store_wars([dict(war_id=identity, war_type="REG", clan_code=CODE, clan_tag=CLAN,
            opponent_tag=OPPONENT, state="warEnded", preparation_start_ts=prep or end - 2 * DAY,
            start_ts=start or end - DAY, end_ts=end, last_seen_ts=end, team_size=1,
            attacks_per_member=2, source="currentwar_api:warEnded")])
        self.repo.store_final_war_rosters([dict(war_id=identity, clan_code=CODE, player_tag="#P2",
            player_name="Synthetic", attacks_expected=2, attacks_used=1,
            roster_state="warEnded", captured_ts=end)])
        self.repo.store_war_attacks([dict(war_id=identity, war_type="REG", clan_code=CODE,
            clan_tag=CLAN, end_ts=end, war_state="warEnded", player_tag="#P2",
            player_name="Synthetic", attack_order=1, defender_tag="#UV", stars=3,
            destruction=100.0, source="currentwar_api:warEnded")])

    def read(self, **selection):
        return self.repo.attendance(clan_codes=[CODE], war_type=selection.pop("war_type", "REG"), **selection)

    def test_early_war_counts_its_lineup_and_final_attacks(self):
        identity = self.capture(END, "warEnded", {"#P2": (2, [1, 2]), "#Q8": (2, [])})
        result = self.read(after_ts=END - DAY, before_ts=END + DAY)
        self.assertEqual(result["war_ids"], [identity])
        rows = {row["player_tag"]: row for row in result["rows"]}
        self.assertEqual({tag: row["missed"] for tag, row in rows.items()}, {"#P2": 0, "#Q8": 2})
        self.assertTrue(all(row["final_capture"] for row in rows.values()))
        self.assertEqual(rows["#P2"]["observed_attack_performance"]["attacks"], 2)
        self.assertEqual(rows["#P2"]["observed_attack_performance"]["stars"], 4)
        self.assertEqual((rows["#P2"]["start_ts"], rows["#P2"]["end_ts"]), (None, END))
        self.assertEqual(result["coverage"]["regular_wars_not_checked_for_friendly"], 1)
        self.assertTrue(result["coverage"]["exact_ranking"])
        self.assertEqual(result["war_aliases"][0]["sources"], ["currentwar_api:warEnded"])
        self.assertEqual(self.read(after_ts=END + 1, before_ts=END + DAY)["war_ids"], [])
        self.assertEqual(self.read(war_type="CWL", after_ts=0, before_ts=END + DAY)["war_ids"], [])

    def test_pause_extended_captures_merge_into_their_latest_final_capture(self):
        early = self.capture(END, "inWar", {"#P2": (2, [1])})
        first = self.capture(END + 1800, "warEnded", {"#P2": (2, [1])})
        latest = self.capture(END + 5400, "warEnded_scheduled", {"#P2": (2, [1, 2])})
        later_war = self.capture(END + 4 * DAY, "warEnded", {"#P2": (2, [1])})
        result = self.read(after_ts=0, before_ts=END + 5 * DAY)
        self.assertEqual(result["war_ids"], sorted([latest, later_war]))
        self.assertEqual(result["duplicate_identities_removed"], 2)
        merged = next(war for war in result["war_aliases"] if war["war_id"] == latest)
        self.assertEqual(merged["aliases"], sorted([early, first, latest]))
        row = next(row for row in result["rows"] if row["war_id"] == latest)
        self.assertEqual((row["attacks_used"], row["missed"]), (2, 0))
        self.assertEqual(row["observed_attack_performance"]["attacks"], 2)
        self.assertEqual(row["end_ts"], END + 5400)
        self.assertEqual(self.read(war_ids=[early])["war_ids"], [latest])

    def test_captures_before_the_war_ended_never_count(self):
        self.capture(END, "inWar", {"#P2": (2, [1])})
        self.capture(END + 10 * DAY, "preparation", {"#P2": (2, [])})
        result = self.read(after_ts=0, before_ts=END + 20 * DAY)
        self.assertEqual((result["war_ids"], result["rows"]), ([], []))
        self.assertEqual(result["coverage"]["regular_wars_not_checked_for_friendly"], 0)

    def test_unreadable_war_id_stays_unproven(self):
        identity = self.capture(END, "warEnded", {"#P2": (2, [1])}, identity="REG:#2Q:not-a-time:#9P")
        result = self.read(after_ts=0, before_ts=END + DAY)
        self.assertEqual(result["unproven_war_ids"], [identity])
        self.assertIsNone(result["rows"][0]["missed"])
        self.assertFalse(result["coverage"]["exact_ranking"])

    def test_wars_with_a_wars_row_are_unchanged(self):
        stored_end = END + 30 * DAY
        stored = war_id(stored_end)
        self.stored_war(stored_end, stored)
        before = {kind: self.read(war_type=kind, after_ts=0, before_ts=END + 60 * DAY)
                  for kind in ("REG", "both", "CWL")}
        early = self.capture(END, "warEnded", {"#P2": (2, [1, 2])})
        # The same war saved under its pre-pause ID must not count again.
        self.capture(stored_end - 3600, "warEnded", {"#P2": (2, [1])})
        for kind in ("REG", "both", "CWL"):
            after = self.read(war_type=kind, after_ts=0, before_ts=END + 60 * DAY)
            self.assertEqual([row for row in after["rows"] if row["war_id"] == stored], before[kind]["rows"])
            self.assertEqual([war for war in after["war_aliases"] if war["war_id"] == stored],
                             before[kind]["war_aliases"])
            self.assertEqual(after["war_ids"], sorted(before[kind]["war_ids"] + ([early] if kind != "CWL" else [])))
        self.assertEqual(before["REG"]["coverage"]["regular_wars_not_checked_for_friendly"], 0)

    def test_read_is_the_same_before_and_after_the_duplicate_war_upgrade(self):
        stored_end = END + 30 * DAY
        times = dict(prep=stored_end - 2 * DAY, start=stored_end - DAY)
        self.stored_war(stored_end - 1800, war_id(stored_end - 1800), **times)
        self.stored_war(stored_end, war_id(stored_end), **times)
        self.capture(END, "inWar", {"#P2": (2, [1])})
        early = self.capture(END + 1800, "warEnded", {"#P2": (2, [1, 2]), "#Q8": (2, [])})
        before = self.read(after_ts=0, before_ts=END + 60 * DAY)
        with closing(sqlite3.connect(self.repo.path)) as connection, connection:
            deduplicate_regular_wars(connection)
        after = self.read(after_ts=0, before_ts=END + 60 * DAY)
        self.assertEqual(after["war_ids"], before["war_ids"])
        self.assertEqual(after["war_ids"], sorted([early, war_id(stored_end)]))
        for field in ("rows", "war_aliases"):
            self.assertEqual([item for item in after[field] if item["war_id"] == early],
                             [item for item in before[field] if item["war_id"] == early])
        self.assertEqual(sum(row["attacks_used"] for row in after["rows"]), 3)
        self.assertEqual(sum(row["attacks_used"] for row in before["rows"]), 3)


if __name__ == "__main__":
    unittest.main()
