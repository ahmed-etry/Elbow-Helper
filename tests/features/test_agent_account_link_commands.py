"""Account link previews keep prior owners and restore them on undo."""

from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.agent.commands.adapters.account_links import (
    prepare_account_add, prepare_account_add_undo,
    prepare_account_remove, run_account_add, run_account_remove,
)


class AccountLinkCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_reassignment_preview_run_and_undo(self):
        links = object.__new__(AccountLinks)
        owner = SimpleNamespace(id=7, mention="<@7>", display_name="New owner")
        old = {"player_tag": "#P0Y", "discord_user_id": 8, "is_primary": 1,
               "player_name_last_seen": "Old name", "last_seen_clan_code": "BEH",
               "last_seen_clan_tag": "#CLAN", "last_seen_role": "member"}
        saved = {"#P0Y": dict(old)}
        links._parse_player_tag_input = lambda _: (["#P0Y"], [])
        links.lookup_players = AsyncMock(return_value=[
            {"player_tag": "#P0Y", "player_name": "New name"},
        ])
        links.get_links_for_user = lambda _: [row for row in saved.values()
                                              if row["discord_user_id"] == owner.id]
        links.get_all_links = lambda: dict(saved)
        links.get_links_by_tags = lambda tags: {tag: saved.get(tag) for tag in tags}
        links.upsert_links = lambda rows: saved.update({
            row["player_tag"]: {
                "player_tag": row["player_tag"],
                "discord_user_id": row["discord_user_id"],
                "is_primary": int(row["is_primary"]),
                "player_name_last_seen": row["player_name_last_seen"],
                "last_seen_clan_code": "", "last_seen_clan_tag": "",
                "last_seen_role": "",
            } for row in rows
        })
        links.restore_links = lambda before: saved.update(before)
        links._try_refresh_linked_boards = AsyncMock()
        links._board_refresher = object()
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: links),
            guild=SimpleNamespace(get_member=lambda _: owner),
        )
        values = {"member": owner.id, "tags": "#P0Y"}
        preview = await prepare_account_add(context, values)
        self.assertTrue(any("from <@8> to <@7>" in line for line in preview.lines))
        self.assertTrue(any("Clear the saved clan" in line for line in preview.lines))
        self.assertTrue(await preview.recheck())
        self.assertEqual(saved["#P0Y"]["discord_user_id"], 8)
        outcome = await run_account_add(context, values)
        self.assertEqual(saved["#P0Y"]["discord_user_id"], 7)
        self.assertFalse(await preview.recheck())
        undo = await prepare_account_add_undo(context, {
            "before": preview.before, "after": outcome.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertEqual(saved["#P0Y"], old)

    async def test_remove_preview_lists_link_and_uses_feature_operation(self):
        links = object.__new__(AccountLinks)
        saved = {"#P0Y": {"player_tag": "#P0Y", "discord_user_id": 8}}
        links._parse_player_tag_input = lambda _: (["#P0Y"], [])
        links.get_all_links = lambda: dict(saved)
        links.get_links_by_tags = lambda tags: {tag: saved.get(tag) for tag in tags}
        links.delete_links = lambda tags: [saved.pop(tag, None) for tag in tags]
        links._try_refresh_linked_boards = AsyncMock()
        links._board_refresher = None
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: links))
        preview = await prepare_account_remove(context, {"tags": "#P0Y"})
        self.assertIn("Unlink `#P0Y` from <@8>.", preview.lines)
        self.assertTrue(await preview.recheck())
        self.assertIn("#P0Y", saved)
        outcome = await run_account_remove(context, {"tags": "#P0Y"})
        self.assertEqual(outcome.visibility, "private")
        self.assertNotIn("#P0Y", saved)


class AccountLinkRestoreTests(unittest.TestCase):
    def test_restore_replaces_new_and_reassigned_links_together(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("elbow_helper.features.account_links.database.DB_PATH",
                       Path(directory) / "links.db"):
                links = AccountLinksDbMixin()
                links._init_db()
                original = {
                    "player_tag": "#P0Y", "discord_user_id": 8,
                    "is_primary": 1, "player_name_last_seen": "Original",
                    "last_seen_clan_tag": "#CLAN",
                    "last_seen_clan_code": "BEH", "last_seen_role": "member",
                }
                links.upsert_links([original])
                links.upsert_links([
                    {"player_tag": "#P0Y", "discord_user_id": 7},
                    {"player_tag": "#LQG", "discord_user_id": 7},
                ])
                links.restore_links({"#P0Y": original, "#LQG": None})
                self.assertEqual(links.get_link_by_tag("#P0Y"), original)
                self.assertIsNone(links.get_link_by_tag("#LQG"))
