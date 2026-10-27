from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.features.recruitment.views import AcceptConfirmationView
from elbow_helper.features.recruitment.views import PersistentEndTrialView


class AcceptConfirmationPermissionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _confirmation(role_id: int):
        cog = SimpleNamespace(complete_accept_confirmation=AsyncMock())
        view = AcceptConfirmationView(cog, payload={"user_id": 456})
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=789, roles=[SimpleNamespace(id=role_id)]),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
        )
        return view, interaction, cog

    async def test_losing_accept_access_blocks_an_open_confirmation(self) -> None:
        for role_id in LEAD | RECRUITERS:
            with self.subTest(role_id=role_id):
                view, interaction, cog = self._confirmation(role_id)
                interaction.user.roles = []

                await view._confirm(interaction)

                cog.complete_accept_confirmation.assert_not_awaited()
                interaction.response.edit_message.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once_with(
                    "You don't have permission to use this command.", ephemeral=True,
                )

    async def test_core_alone_cannot_confirm_acceptance(self) -> None:
        for role_id in CORE:
            with self.subTest(role_id=role_id):
                view, interaction, cog = self._confirmation(role_id)

                await view._confirm(interaction)

                cog.complete_accept_confirmation.assert_not_awaited()
                interaction.response.edit_message.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once_with(
                    "You don't have permission to use this command.", ephemeral=True,
                )

    async def test_each_accept_role_can_confirm(self) -> None:
        for role_id in LEAD | RECRUITERS:
            with self.subTest(role_id=role_id):
                view, interaction, cog = self._confirmation(role_id)

                await view._confirm(interaction)

                cog.complete_accept_confirmation.assert_awaited_once_with(
                    interaction, view.payload,
                )
                interaction.response.edit_message.assert_awaited_once_with(view=None)
                interaction.response.send_message.assert_not_awaited()


class PersistentEndTrialViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolving_a_trial_reminder_uses_the_embed_as_confirmation(self) -> None:
        view = PersistentEndTrialView(ticket_channel_id=123, applicant_id=456)
        cog = MagicMock()
        cog._get_trial_reminder_entry = AsyncMock(return_value=None)
        cog.end_trial_now = AsyncMock(return_value=True)
        interaction = MagicMock()
        interaction.client.get_cog.return_value = cog
        interaction.user.id = 789

        await view.end_trial_callback(interaction)

        cog.end_trial_now.assert_awaited_once_with(
            interaction,
            123,
            456,
            allow_missing=True,
            show_success_confirmation=False,
            resolve_reminder=True,
        )


if __name__ == "__main__":
    unittest.main()
