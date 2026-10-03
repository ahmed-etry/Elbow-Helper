"""Player-tag searching uses the domain's canonical tag rules."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.domain.player_tags import find_player_tags
from elbow_helper.features.agent.plan.sources import requested_clans, requested_player_tags


class PlayerTagSearchTests(unittest.TestCase):
    def test_search_normalizes_distinct_standalone_tags(self):
        text = "Check #p0, #P0 and #q2v. Ignore #INVALID, prefix#P0, ##Q2 and #Q2_name."
        self.assertEqual(find_player_tags(text), frozenset({"#P0", "#Q2V"}))
        self.assertEqual(find_player_tags("#" + "P" * 16), frozenset())

    def test_request_sources_separate_accounts_from_known_clan_tags(self):
        clans = {"AAA": SimpleNamespace(code="AAA", tag="#P0")}
        with (patch("elbow_helper.features.agent.plan.sources.CLANS", clans),
              patch("elbow_helper.features.agent.plan.sources.CLAN_ORDER", ("AAA",))):
            self.assertEqual(requested_clans("#p0 #Q2V"), frozenset({"AAA"}))
            self.assertEqual(requested_player_tags("#p0 #Q2V"), frozenset({"#Q2V"}))
