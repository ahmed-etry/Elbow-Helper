from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.features.hibernation.commands import HibernationCommandMixin
from elbow_helper.features.hibernation.state import save_hibernation_state


class HibernationPersistenceTests(unittest.TestCase):
    def test_state_write_failure_is_reported_to_the_workflow(self) -> None:
        with patch(
            "elbow_helper.features.hibernation.state.write_json_atomic",
            side_effect=OSError("disk full"),
        ):
            with (
                self.assertLogs(
                    "elbow_helper.features.hibernation.state",
                    level="ERROR",
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                save_hibernation_state({"42": {"roles": [1]}})


class HibernationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_failure_stops_before_roles_change(self) -> None:
        owner = HibernationCommandMixin()
        member = SimpleNamespace(
            id=42,
            mention="<@42>",
            roles=[],
            remove_roles=AsyncMock(),
            add_roles=AsyncMock(),
        )
        actor = SimpleNamespace(roles=[SimpleNamespace(id=99)])
        interaction = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with (
            patch(
                "elbow_helper.features.hibernation.commands.LEAD_PLUS",
                frozenset({99}),
            ),
            patch(
                "elbow_helper.features.hibernation.commands.load_hibernation_state",
                return_value={},
            ),
            patch(
                "elbow_helper.features.hibernation.commands.save_hibernation_state",
                side_effect=OSError("disk full"),
            ),
        ):
            with self.assertLogs(
                "elbow_helper.features.hibernation.commands",
                level="ERROR",
            ):
                await HibernationCommandMixin.hibernate_user.callback(
                    owner,
                    interaction,
                    member,
                )

        member.remove_roles.assert_not_awaited()
        member.add_roles.assert_not_awaited()
        self.assertIn(
            "couldn't move that member into hibernation",
            interaction.followup.send.await_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
