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


    def test_two_spellings_of_one_tag_are_an_incomplete_roster(self) -> None:
        with self.assertLogs("elbow_helper.features.cwl.router", level="WARNING"):
            self.assertIsNone(self.extract(_war({"tag": "#P0Y"}, {"tag": "#POY"})))

    def test_member_with_an_invalid_or_missing_tag_is_an_incomplete_roster(self) -> None:
        for bad in ({"tag": "#ZZZ"}, {"name": "No tag"}, {"tag": ""}, "not a member"):
            with self.subTest(bad=bad), self.assertLogs("elbow_helper.features.cwl.router", level="WARNING"):
                self.assertIsNone(self.extract(_war({"tag": "#P0Y"}, bad, team_size=2)))


if __name__ == "__main__":
    unittest.main()
