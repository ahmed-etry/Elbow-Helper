from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.recruitment.views import PersistentEndTrialView


class PersistentEndTrialViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolving_a_trial_reminder_uses_the_embed_as_confirmation(self) -> None:
        view = PersistentEndTrialView(ticket_channel_id=123, applicant_id=456)
        cog = MagicMock()
        cog._get_trial_reminder_entry = AsyncMock(return_value=None)
        cog.end_trial_now = AsyncMock(return_value=True)
        cog._mark_trial_reminder_resolved = AsyncMock()
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
        )
        cog._mark_trial_reminder_resolved.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
