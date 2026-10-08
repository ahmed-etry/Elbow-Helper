"""Player-tag searching uses the domain's canonical tag rules."""
import unittest

from elbow_helper.domain.player_tags import find_player_tags


class PlayerTagSearchTests(unittest.TestCase):
    def test_search_normalizes_distinct_standalone_tags(self):
        text = "Check #p0, #P0 and #q2v. Ignore #INVALID, prefix#P0, ##Q2 and #Q2_name."
        self.assertEqual(find_player_tags(text), frozenset({"#P0", "#Q2V"}))
        self.assertEqual(find_player_tags("#" + "P" * 16), frozenset())
