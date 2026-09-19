from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.wars.board import WarBoardMixin
from elbow_helper.features.wars.queries import WarQueries


def _war(state="inWar", *, members=2):
    roster = []
    tags = ("#P0", "#P2")
    for index in range(members):
        attacks = [] if index else [{"stars": 3, "destructionPercentage": 100}]
        roster.append({
            "tag": tags[index], "name": f"Player {index + 1}",
            "townhallLevel": 18, "mapPosition": index + 1, "attacks": attacks,
        })
    return {
        "state": state, "teamSize": 2, "attacksPerMember": 2,
        "preparationStartTime": "20260915T000000.000Z",
        "startTime": "20260916T000000.000Z",
        "endTime": "20260917T000000.000Z",
        "clan": {"tag": "#P0", "name": "Hellbow", "stars": 3,
                 "destructionPercentage": 50.0, "members": roster},
        "opponent": {"tag": "#P8", "name": "Opponent", "stars": 2,
                     "destructionPercentage": 40.0, "members": []},
    }


class WarQueryTests(unittest.TestCase):
    def manager(self, history=None, observations=None):
        return SimpleNamespace(
            war_board_history=history or {}, war_observations=observations or {},
        )

    def test_observed_current_snapshot_separates_remaining_from_missed_attacks(self):
        payload = _war("inWar")
        manager = self.manager(
            {"BEH": {"current": payload}},
            {"BEH": {"state": "inwar", "observed_at": "now",
                     "war_id": "20260915T000000.000Z-20260917T000000.000Z-#P8"}},
        )
        snapshot = WarQueries(manager).snapshot("BEH")
        self.assertEqual(snapshot.evidence_status, "observed")
        self.assertEqual([row.attacks_remaining for row in snapshot.members], [1, 2])
        self.assertEqual([row.missed_attacks for row in snapshot.members], [0, 0])
        self.assertTrue(snapshot.roster_complete)

    def test_previous_ended_snapshot_reports_exact_missed_counts_and_result(self):
        payload = _war("warEnded")
        manager = self.manager({"BEH": {"previous": payload}})
        snapshot = WarQueries(manager).snapshot("BEH", selected="previous")
        self.assertEqual(snapshot.evidence_status, "stored_previous")
        self.assertEqual([row.missed_attacks for row in snapshot.members], [1, 2])
        self.assertEqual(snapshot.result, "won")

    def test_status_distinguishes_observation_states_from_cached_evidence(self):
        cached = _war("warEnded")
        manager = self.manager(
            {"BEH": {"current": cached, "previous": deepcopy(cached)},
             "BE4": {"current": _war("preparation")}},
            {"BEH": {"state": "notinwar", "observed_at": "one", "war_id": None},
             "BE4": {"state": "cwl", "observed_at": "two", "war_id": None}},
        )
        queries = WarQueries(manager)
        self.assertEqual(queries.status("BEH").evidence_status, "not_in_war")
        self.assertIsNone(queries.status("BEH").current_war_id)
        self.assertIsNotNone(queries.status("BEH").previous_war_id)
        self.assertEqual(queries.status("BE4").evidence_status, "cwl_active")
        self.assertEqual(queries.status("BES").evidence_status, "unavailable")

        manager.war_observations.clear()
        self.assertEqual(queries.status("BEH").evidence_status, "cached_unverified")

    def test_incomplete_and_malformed_rosters_are_not_silently_complete(self):
        incomplete = _war("warEnded", members=1)
        snapshot = WarQueries(self.manager({"BEH": {"previous": incomplete}})).snapshot(
            "BEH", selected="previous",
        )
        self.assertFalse(snapshot.roster_complete)
        self.assertEqual(snapshot.issues, ("incomplete_roster:1/2",))

        malformed = _war("warEnded")
        malformed["clan"]["members"][1]["tag"] = "bad"
        with self.assertRaises(ValueError):
            WarQueries(self.manager({"BEH": {"previous": malformed}})).snapshot(
                "BEH", selected="previous",
            )


class WarObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_poll_records_not_in_war_and_cwl_without_replacing_history(self):
        manager = WarBoardMixin()
        manager.war_observations = {}
        manager.war_board_history = {"BEH": {"current": _war("warEnded")}}
        manager._remove_war_board_controls = AsyncMock()

        await manager._update_war_board("Hellbow", {"state": "notInWar"})
        self.assertEqual(manager.war_observations["BEH"]["state"], "notinwar")
        self.assertIn("current", manager.war_board_history["BEH"])

        await manager._update_war_board(
            "Hellbow", {"state": "inWar", "warTag": "#P0"},
        )
        self.assertEqual(manager.war_observations["BEH"]["state"], "cwl")
        manager._remove_war_board_controls.assert_awaited()


if __name__ == "__main__":
    unittest.main()
