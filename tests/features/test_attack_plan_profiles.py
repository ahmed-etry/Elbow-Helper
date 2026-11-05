"""Attack plans read the account through the collectors and keep their replies."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.attack_plans.api import fetch_player
from elbow_helper.features.attack_plans.cog import Planning
from elbow_helper.infrastructure.clash import ClashResponse

from tests.features.profile_source import profile_demand


class AttackPlanProfileTests(unittest.IsolatedAsyncioTestCase):
    def profiles(self, *replies, configured=True):
        client = SimpleNamespace(configured=configured, get=AsyncMock(side_effect=list(replies)))
        return profile_demand(client), client

    async def test_the_profile_keeps_its_request_details(self):
        profiles, client = self.profiles(ClashResponse(200, {"tag": "#2PP", "name": "Planner"}, {}, 2, 40))
        data = await fetch_player("#2PP", profiles)
        self.assertEqual(data, {"tag": "#2PP", "name": "Planner", "_http_status": 200,
                                "_latency_ms": 40, "_attempts": 2})
        self.assertEqual(client.get.await_args.args, ("/players/%232PP",))
        self.assertEqual(client.get.await_args.kwargs,
                         {"fresh": True, "attempts": 3, "timeout_seconds": 10, "backoff_seconds": 0.75})
        # A profile from the last few minutes is reused; an older one is asked for again.
        self.assertEqual((await fetch_player("#2PP", profiles))["name"], "Planner")
        client.get.assert_awaited_once()
        client.get.side_effect = [ClashResponse(200, {"tag": "#2PP", "name": "Later"}, {}, 1, 40)]
        with patch("elbow_helper.data.collectors.demand.now_us", return_value=10**18):
            self.assertEqual((await fetch_player("#2PP", profiles))["name"], "Later")

    async def test_failures_keep_the_same_replies(self):
        planning = object.__new__(Planning)
        cases = (
            (ClashResponse(404, {"reason": "notFound"}, {}, 1, 5), "That player couldn't be found."),
            (ClashResponse(403, {"reason": "accessDenied"}, {}, 1, 5), "Clash data is unavailable."),
            (ClashResponse(503, {"reason": "maintenance"}, {}, 3, 5),
             "Clash data isn't available right now. Try again later."),
            (ClashResponse(200, None, {}, 3, 5, ValueError("not json")),
             "Clash data isn't available right now. Try again later."),
        )
        for reply, text in cases:
            with self.subTest(status=reply.status):
                profiles, _ = self.profiles(reply)
                data = await fetch_player("#2PP", profiles)
                self.assertEqual(data["_http_status"], reply.status)
                self.assertEqual(Planning._player_fetch_error_text(data), text)
        planning.profiles, client = self.profiles(configured=False)
        self.assertIsNone(await fetch_player("#2PP", planning.profiles))
        client.get.assert_not_awaited()
        self.assertEqual(Planning._player_fetch_error_text(None), "Clash data isn't available right now. Try again later.")

    async def test_a_failed_lookup_gets_the_commands_own_reply(self):
        unavailable = "Clash data isn't available right now. Try again later."
        cases = (
            ("network failure", ClashResponse(None, None, {}, 3, None, ConnectionError("down")), unavailable),
            ("timeout", ClashResponse(None, None, {}, 3, None, TimeoutError()), unavailable),
            ("unreadable reply", ClashResponse(200, None, {}, 3, 5, ValueError("not json")), unavailable),
            ("not found", ClashResponse(404, {"reason": "notFound"}, {}, 1, 5), "That player couldn't be found."),
            ("maintenance", ClashResponse(503, {"reason": "maintenance"}, {}, 3, 5), unavailable),
        )
        image = SimpleNamespace(content_type="image/png")
        for name, reply, text in cases:
            with self.subTest(name):
                planning = object.__new__(Planning)
                planning.profiles, _ = self.profiles(reply)
                planning.plan_emojis = SimpleNamespace(get=AsyncMock())
                prepared = await planning.prepare_attack_plan("#2PP", "plan", image, image)
                self.assertEqual(prepared, {"issue": text, "player_tag": "#2PP"})
                planning.plan_emojis.get.assert_not_awaited()

    async def test_missing_clash_access_is_reported_before_asking(self):
        planning = object.__new__(Planning)
        planning.profiles, client = self.profiles(configured=False)
        image = SimpleNamespace(content_type="image/png")
        self.assertEqual(planning.validate_plan_inputs("#2PP", image, image),
                         (None, "Clash data isn't available because the connection hasn't been set up."))
        client.get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
