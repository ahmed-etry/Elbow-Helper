from __future__ import annotations

import unittest

from elbow_helper.features.cwl.router import CwlRouterMixin


CLAN_TAG = "#2PP"


def _war(*members: dict, team_size: int | None = None) -> dict:
    return {
        "teamSize": len(members) if team_size is None else team_size,
        "clan": {"tag": CLAN_TAG, "members": list(members)},
        "opponent": {"tag": "#8YL", "members": []},
    }


class CompleteRosterTagTests(unittest.TestCase):
    def extract(self, war: dict):
        return CwlRouterMixin()._extract_complete_roster(war, CLAN_TAG)

    def test_tags_are_canonical_whatever_the_written_form(self) -> None:
        roster, names = self.extract(_war(
            {"tag": " #p0y ", "name": "First"}, {"tag": "q2l"}, {"tag": "#GOY", "name": "Third"},
        ))

        self.assertEqual(roster, {"#P0Y", "#Q2L", "#G0Y"})
        self.assertEqual(names, {"#P0Y": "First", "#Q2L": "#Q2L", "#G0Y": "Third"})

    def test_valid_tags_are_unchanged(self) -> None:
        roster, names = self.extract(_war({"tag": "#P0Y", "name": "First"}, {"tag": "#Q2L", "name": "Second"}))

        self.assertEqual(roster, {"#P0Y", "#Q2L"})
        self.assertEqual(names, {"#P0Y": "First", "#Q2L": "Second"})

    def test_two_spellings_of_one_tag_are_an_incomplete_roster(self) -> None:
        with self.assertLogs("elbow_helper.features.cwl.router", level="WARNING"):
            self.assertIsNone(self.extract(_war({"tag": "#P0Y"}, {"tag": "#POY"})))

    def test_member_with_an_invalid_or_missing_tag_is_an_incomplete_roster(self) -> None:
        for bad in ({"tag": "#ZZZ"}, {"name": "No tag"}, {"tag": ""}, "not a member"):
            with self.subTest(bad=bad), self.assertLogs("elbow_helper.features.cwl.router", level="WARNING"):
                self.assertIsNone(self.extract(_war({"tag": "#P0Y"}, bad, team_size=2)))


if __name__ == "__main__":
    unittest.main()
