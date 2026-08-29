from __future__ import annotations

import unittest

from elbow_helper.features.achievements.commands import AchievementCommandMixin
from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.catalog import HELP_INDEX
from elbow_helper.features.help.catalog import CAT_CWL
from elbow_helper.features.help.catalog import CAT_ROSTERS
from elbow_helper.features.help.catalog import HelpEntry
from elbow_helper.features.help.discovery import DiscoveredCommand
from elbow_helper.features.help.discovery import ParameterInfo
from elbow_helper.features.help.rendering import build_detail_embed
from elbow_helper.features.help.rendering import build_list_embed


class HelpCatalogTests(unittest.TestCase):
    def test_catalog_paths_are_unique_and_copy_is_complete(self) -> None:
        paths = [entry.path for entry in HELP_ENTRIES]

        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("/help", paths)
        for entry in HELP_ENTRIES:
            self.assertTrue(entry.summary.strip(), entry.path)
            self.assertTrue(entry.details.strip(), entry.path)
            self.assertLessEqual(len(entry.summary), 1024, entry.path)
            self.assertLessEqual(len(entry.details), 4096, entry.path)
            for example in entry.examples:
                self.assertTrue(example.startswith(entry.path), f"{entry.path}: {example}")

    def test_command_list_uses_discovery_summary_only(self) -> None:
        entry = HelpEntry(
            path="/example",
            summary="Find the command you need.",
            details="Instructions shown only on the command page.",
            category="General",
        )

        embed = build_list_embed([entry], page=0, page_size=8, title="Commands You Can Use")

        self.assertEqual(embed.title, "Commands You Can Use")
        self.assertIsNone(embed.thumbnail.url)
        self.assertEqual(embed.fields[0].name, "/example")
        self.assertEqual(embed.fields[0].value, "Find the command you need.")
        self.assertNotIn("Instructions", embed.fields[0].value)

    def test_detail_page_uses_standalone_command_guidance(self) -> None:
        entry = HelpEntry(
            path="/example",
            summary="Build the requested report.",
            details="Choose an account, then select the period to review.",
            category="Reports",
            examples=("/example account:#PLAYER", "/example account:#PLAYER period:Last 14 days"),
            notes=("The period defaults to the last 30 days.",),
        )
        discovered = DiscoveredCommand(
            path=entry.path,
            description=entry.summary,
            parameters=(
                ParameterInfo(
                    name="account",
                    description="Clash account to include.",
                    required=True,
                    type_name="string",
                ),
                ParameterInfo(
                    name="period",
                    description="Period to review.",
                    required=False,
                    type_name="string",
                    choices=("Last 14 days", "Last 30 days"),
                ),
            ),
        )

        embed = build_detail_embed(entry, discovered)

        self.assertEqual(
            embed.description,
            "Choose an account, then select the period to review.",
        )
        self.assertIsNone(embed.thumbnail.url)
        self.assertEqual([field.name for field in embed.fields], ["Options", "Examples", "Notes"])
        self.assertIn("`account` — Clash account to include.", embed.fields[0].value)
        self.assertIn("`period` *(optional)* — Period to review.", embed.fields[0].value)
        self.assertIn("Choices: Last 14 days, Last 30 days", embed.fields[0].value)
        self.assertEqual(
            embed.fields[1].value,
            "```\n/example account:#PLAYER\n/example account:#PLAYER period:Last 14 days\n```",
        )

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

    def test_detail_page_marks_missing_command_options(self) -> None:
        entry = HelpEntry(
            path="/example",
            summary="Run the example.",
            details="",
            category="General",
        )

        embed = build_detail_embed(entry, discovered=None)

        self.assertEqual(embed.footer.text, "Command options couldn't be loaded.")

    def test_inventory_help_keeps_self_service_summary_and_examples(self) -> None:
        entry = HELP_INDEX["/inventory"]
        user_option = AchievementCommandMixin.inventory.parameters[0]

        self.assertEqual(entry.summary, "Check your coins and raffle ticket.")
        self.assertIn("your coin balance", entry.details)
        self.assertIn("Leadership", entry.details)
        self.assertEqual(entry.examples, ("/inventory", "/inventory user:@User"))
        self.assertEqual(
            user_option.description,
            "Leadership can choose another member. Leave empty to view your own inventory.",
        )
        self.assertFalse(user_option.required)

    def test_non_obvious_workflows_keep_additional_guidance(self) -> None:
        self.assertEqual(
            HELP_INDEX["/record edit"].details,
            (
                "Opens controls for choosing one of the member's recent records and changing "
                "its category, incident type, or details."
            ),
        )
        self.assertEqual(
            HELP_INDEX["/connections"].details,
            (
                "Posts the role connections board in this channel. Rules can add or remove roles "
                "based on other roles a member has or lacks."
            ),
        )

    def test_roster_help_matches_current_limits_and_output(self) -> None:
        self.assertEqual(HELP_INDEX["/roster announcement"].category, CAT_CWL)
        self.assertTrue(
            all(
                entry.category == CAT_ROSTERS
                for entry in HELP_ENTRIES
                if entry.path.startswith("/roster ")
                and entry.path != "/roster announcement"
            )
        )
        self.assertIn("500 accounts", HELP_INDEX["/roster create"].details)
        self.assertNotIn("TH8", HELP_INDEX["/roster create"].details)
        self.assertNotIn("TH minimum", HELP_INDEX["/roster edit"].summary)
        self.assertIn("signup timing", HELP_INDEX["/roster post"].details)
        self.assertIn("while signup controls are shown", HELP_INDEX["/roster post"].details)
        self.assertIn("page", HELP_INDEX["/roster post"].details)
        self.assertIn("Player column is always shown", HELP_INDEX["/roster post"].notes[0])
        self.assertIn("can be hidden", HELP_INDEX["/roster post"].notes[0])
        self.assertIn("affect Discord only", HELP_INDEX["/roster post"].notes[1])
        self.assertIn("Google Sheets keeps every column", HELP_INDEX["/roster post"].notes[1])
        self.assertIn("one opening", HELP_INDEX["/roster timing"].summary)
        self.assertIn("close_day:last-1", HELP_INDEX["/roster schedule"].examples[0])
        self.assertIn("open_day:last-1", HELP_INDEX["/roster schedule"].examples[1])
        self.assertIn("following month", HELP_INDEX["/roster schedule"].details)
        self.assertIn("`1`–`28`", HELP_INDEX["/roster schedule"].details)
        self.assertIn("`last-2`", HELP_INDEX["/roster schedule"].details)
        self.assertNotIn("`last-N`", HELP_INDEX["/roster schedule"].details)
        self.assertNotIn("last-x", HELP_INDEX["/roster schedule"].details)
        self.assertIn("first schedule needs opening and closing", HELP_INDEX["/roster schedule"].notes[0])
        self.assertIn("CWL signup roster", HELP_INDEX["/roster schedule"].notes[1])
        self.assertIn("announcement and reminders", HELP_INDEX["/roster timing"].details)
        self.assertIn("enabled:false", HELP_INDEX["/roster schedule"].examples[2])
        self.assertIn("before posting", HELP_INDEX["/roster schedule"].notes[2])
        self.assertIn("wherever the roster is posted", HELP_INDEX["/roster schedule"].notes[2])
        self.assertEqual(HELP_INDEX["/roster post"].summary, "Post a roster in this channel.")
        self.assertIn("same roster stays in sync", HELP_INDEX["/roster post"].details)
        self.assertIn("changed while cloning", HELP_INDEX["/roster clone"].details)
        self.assertIn("min_townhall:16", HELP_INDEX["/roster clone"].examples[1])
        self.assertIn("combined hero level", HELP_INDEX["/roster export"].details)
        self.assertIn("opened in Google Sheets or downloaded", HELP_INDEX["/roster export"].details)
        self.assertNotIn(
            "max_members:150",
            "\n".join(HELP_INDEX["/roster edit"].examples),
        )

    def test_help_does_not_describe_reply_visibility_without_a_user_need(self) -> None:
        for path in (
            "/opinion",
            "/record edit",
            "/event panel",
            "/event list",
            "/roster list",
        ):
            self.assertNotIn("private", HELP_INDEX[path].details.lower(), path)

    def test_raffle_help_matches_the_current_draw_workflow(self) -> None:
        self.assertEqual(
            HELP_INDEX["/raffle reroll"].details,
            "Runs this month's raffle draw again and announces the result.",
        )
        self.assertNotIn("replace", HELP_INDEX["/raffle history"].details.lower())
        self.assertIn(
            "`clear_tickets:true`",
            HELP_INDEX["/raffle clear"].details,
        )


if __name__ == "__main__":
    unittest.main()
