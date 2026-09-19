from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from elbow_helper.configuration.roles import ELDER_ROLE_ID, LEAD_PLUS
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.clan_reporting.queries import ClanReportingQueries


class ClanReportingQueryTests(unittest.TestCase):
    def test_missing_elder_source_reacts_to_current_discord_roles(self):
        member = SimpleNamespace(
            id=42, display_name="Alpha",
            roles=[SimpleNamespace(id=ELDER_ROLE_ID)],
        )
        guild = SimpleNamespace(get_member=lambda member_id: (
            member if member_id == 42 else None
        ))
        owner = object.__new__(AccountLinks)
        owner.bot = SimpleNamespace(get_guild=lambda _: guild)
        owner._clan_members = {"BEH": {"#P2LQ": {
            "player_tag": "#P2LQ", "player_name": "One", "role": "member",
        }}}
        owner.get_link_by_tag = lambda _: {"discord_user_id": 42}

        self.assertEqual(len(owner.get_missing_elder_rows("BEH")), 1)
        member.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        self.assertEqual(owner.get_missing_elder_rows("BEH"), [])
        member.roles = []
        self.assertEqual(owner.get_missing_elder_rows("BEH"), [])

    def test_missing_elder_snapshot_reuses_rows_and_reports_invalid_coverage(self):
        rows = {
            "BEH": [
                {
                    "discord_user_id": 42,
                    "discord_display_name": "Alpha",
                    "player_tag": "#P2LQ",
                    "player_name": "One",
                    "clan_code": "BEH",
                    "ingame_role": "member",
                },
                {"discord_user_id": "bad", "clan_code": "BEH"},
            ],
            "BE4": [{
                "discord_user_id": 43,
                "discord_display_name": "Beta",
                "player_tag": "#Y8J9",
                "player_name": "Two",
                "clan_code": "BE4",
                "ingame_role": "member",
            }],
        }
        queries = ClanReportingQueries(
            lambda code: rows[code],
            clock=lambda: datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
        )

        snapshot = queries.missing_elder_snapshot(("BEH", "BE4"))

        self.assertEqual(snapshot.observed_at, "2026-09-19T12:00:00+00:00")
        self.assertEqual(snapshot.selected_clan_codes, ("BEH", "BE4"))
        self.assertEqual(snapshot.skipped_invalid_row_count, 1)
        self.assertEqual([row.member_id for row in snapshot.rows], [42, 43])
        self.assertEqual(snapshot.rows[0].player_tag, "#P2LQ")

    def test_duplicate_account_and_wrong_clan_are_skipped(self):
        rows = [{
            "discord_user_id": 42,
            "discord_display_name": "Alpha",
            "player_tag": "#P2LQ",
            "player_name": "One",
            "clan_code": "BEH",
            "ingame_role": "member",
        }]
        queries = ClanReportingQueries(
            lambda code: rows if code == "BEH" else [
                {**rows[0], "clan_code": "BEH"},
            ],
        )
        snapshot = queries.missing_elder_snapshot(("BEH", "BE4"))
        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(snapshot.skipped_invalid_row_count, 1)

    def test_unknown_or_oversize_scope_is_rejected(self):
        queries = ClanReportingQueries(lambda _: ())
        for clan_codes in ((), ("UNKNOWN",), ("BEH", "BEH")):
            with self.subTest(clan_codes=clan_codes):
                with self.assertRaises(ValueError):
                    queries.missing_elder_snapshot(clan_codes)


if __name__ == "__main__":
    unittest.main()
