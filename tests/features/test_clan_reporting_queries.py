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
        owner.get_all_links = lambda: {"#P2LQ": {"discord_user_id": 42}}

        self.assertEqual(len(owner.get_missing_elder_rows("BEH")), 1)
        member.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        self.assertEqual(owner.get_missing_elder_rows("BEH"), [])
        member.roles = []
        self.assertEqual(owner.get_missing_elder_rows("BEH"), [])


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


if __name__ == "__main__":
    unittest.main()
