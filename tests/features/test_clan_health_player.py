from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.config import UTC
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin


class RegularWarsStub:
    """The family record's regular wars as clan health asks for them."""

    def __init__(self, activity=None, counts=None):
        self.activity_rows = activity or {}
        self.counts = counts or {}
        self.calls = []

    def activity(self, **window):
        self.calls.append(("activity", window))
        return {tag: row for tag, row in self.activity_rows.items() if tag in window["player_tags"]}

    def war_counts(self, *, clan_codes=None, **window):
        self.calls.append(("war_counts", {**window, "clan_codes": clan_codes}))
        return {code: count for code, count in self.counts.items() if clan_codes is None or code in clan_codes}


def regular(wars, expected, used, *, stars=0, destruction=0.0, measured=0, unknown=0):
    return {"wars": wars, "attacks_expected": expected, "attacks_used": used, "stars": stars,
            "destruction": destruction, "attacks_measured": measured, "wars_without_counts": unknown}


class FamilyWarParticipationTests(unittest.TestCase):

    def test_a_regular_war_with_unknown_counts_is_joined_but_never_missed(self):
        stub = RegularWarsStub({"#P0": regular(1, 0, 0, unknown=1)})
        rows = [{"player_tag": "#P0", "clan_code": "BES"}]
        start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
        ClanHealthAnalyzer(SimpleNamespace(), stub, RegularWarsStub())._apply_family_war_activity(
            rows, cycle_start=start, cycle_end=end)
        self.assertEqual((rows[0]["regular_war_events_joined"], rows[0]["war_hits_expected"], rows[0]["war_missed"]),
                         (1, 0, 0))

    def test_home_and_extra_wars_do_not_share_a_home_only_denominator(self):
        repository = SimpleNamespace()
        stub = RegularWarsStub({"#P0": regular(15, 30, 30)}, {"BES": 5, "BEW": 10})
        analyzer = ClanHealthAnalyzer(repository, stub, RegularWarsStub())
        rows = [{"player_tag": "#P0", "player_name": "Synthetic", "clan_code": "BES"}]
        start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
        analyzer._apply_family_war_activity(rows, cycle_start=start, cycle_end=end)
        rules = {"_profile_name": "competitive", "war": {
            "wars_to_join": 5, "missed_attack_rate_percent": 0,
        }}
        with patch("elbow_helper.features.clan_health.analysis.flags.effective_player_rules",
                   return_value=rules):
            analyzer._apply_flags(rows, cycle_start=start, cycle_end=end)
        # Only the player's own clan's wars set the regular-war denominator.
        self.assertEqual(stub.calls[-1], ("war_counts", {"cycle_start_ts": 1, "cycle_end_ts": 200,
                                                         "clan_codes": {"BES"}}))
        row = rows[0]
        self.assertEqual(row["regular_war_events_joined"], 15)
        self.assertEqual((row["regular_war_hits_expected"], row["regular_war_hits_used"]), (30, 30))
        card = row["signal_cards"]["war_attendance"]
        expected = "Joined 15 regular wars across the family"
        self.assertEqual(card["current"], expected)
        self.assertEqual(ClanHealthPlayerCommandMixin._signal_summary_text(card), expected)
        self.assertEqual(card["verdict"], "Good")
        self.assertEqual(row["war_hit_usage_verdict"], "Good")
