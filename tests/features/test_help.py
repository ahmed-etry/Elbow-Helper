from __future__ import annotations

import unittest
from types import SimpleNamespace

from elbow_helper.configuration.roles import CORE, LEAD_PLUS, MEMBER_ROLE_ID, RECRUITERS
from elbow_helper.features.achievements.economy import AchievementEconomyMixin
from elbow_helper.features.help.catalog import HelpEntry
from elbow_helper.features.help.cog import HelpCog
from elbow_helper.features.help.discovery import DiscoveredCommand
from elbow_helper.features.help.rendering import build_detail_embed


class HelpVisibilityTests(unittest.IsolatedAsyncioTestCase):
    economy_commands = (
        "/grant coins",
        "/grant ticket",
        "/raffle list",
        "/raffle draw",
        "/raffle reroll",
        "/raffle history",
    )

    def test_economy_list_and_autocomplete_match_command_permission(self) -> None:
        cog = HelpCog(bot=None)
        economy = AchievementEconomyMixin()
        for role_id in (*CORE, *LEAD_PLUS, *RECRUITERS, MEMBER_ROLE_ID, None):
            interaction = SimpleNamespace(
                user=SimpleNamespace(
                    roles=[] if role_id is None else [SimpleNamespace(id=role_id)],
                ),
            )
            allowed = economy._require_command_role(interaction)
            visible = {entry.path for entry in cog._visible_entries(interaction)}
            for path in self.economy_commands:
                with self.subTest(role_id=role_id, path=path):
                    self.assertEqual(path in visible, allowed)
                    choices = cog._autocomplete_choices(interaction, current=path)
                    self.assertEqual(path in {choice.value for choice in choices}, allowed)


class HelpCatalogTests(unittest.TestCase):


    def test_detail_page_falls_back_to_summary_when_guidance_is_missing(self) -> None:
        entry = HelpEntry(
            path="/example",
            summary="Run the one-step action.",
            details="",
            category="General",
        )
        discovered = DiscoveredCommand(
            path=entry.path,
            description=entry.summary,
            parameters=(),
        )

        embed = build_detail_embed(entry, discovered)

        self.assertEqual(embed.description, "Run the one-step action.")
        self.assertEqual(embed.fields, [])
        self.assertIsNone(embed.footer.text)


if __name__ == "__main__":
    unittest.main()
