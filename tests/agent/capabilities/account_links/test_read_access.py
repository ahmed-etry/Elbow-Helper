"""Confirmed account links are readable without leadership access."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from elbow_helper.configuration.roles import AGENT_TESTER_ROLE_ID
from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.capabilities.account_links.data import SOURCES
from elbow_helper.agent.capabilities.account_links.reads import (
    account_read_tools, read_linked_accounts,
)
from elbow_helper.agent.datasets.query import run_query
from elbow_helper.agent.delivery import AgentDeliveryMixin
from agent.engine.test_agent_plan_flow import _context


class AccountLinkReadAccessTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = _context()
        context.member.roles[:] = [SimpleNamespace(id=AGENT_TESTER_ROLE_ID)]
        context.source_message.archive_reply = False
        context.source_message.mentions = ()
        context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=501))
        context.source_message.guild = context.guild
        return replace(context, account_links=SimpleNamespace(
            get_links_for_members=Mock(return_value={
                101: [{"player_tag": "#P0", "player_name_last_seen": "Example"}],
            }),
        ))

    async def test_lookup_and_delivery_need_no_leadership_access(self):
        context = self.context()
        result = await read_linked_accounts(context, {"member_ids": [101, 102]})
        self.assertEqual(result["accounts"], [
            {"member_id": 101, "player_tag": "#P0", "player_name": "Example"},
        ])
        self.assertEqual(result["unlinked_member_ids"], [102])
        self.assertFalse(context.state.required_access)
        self.assertFalse(account_read_tools()[0].contract.required_access)
        delivery = AgentDeliveryMixin()
        delivery.bot = context.bot
        await delivery.send_response(context.source_message, "Example: #P0", None, context=context)
        self.assertEqual(context.source_message.reply.await_args.args[0], "Example: #P0")
        self.assertIsNone(context.source_message.reply.await_args.kwargs.get("view"))

    async def test_existing_restricted_evidence_is_not_cleared(self):
        context = self.context()
        context.state.required_access.add("lead")
        with self.assertRaises(AgentAccessLost):
            await read_linked_accounts(context, {"member_ids": [101]})
        context.account_links.get_links_for_members.assert_not_called()
        self.assertEqual(context.state.required_access, {"lead"})

    async def test_entry_access_is_still_required(self):
        context = self.context()
        context.member.roles.clear()
        with self.assertRaises(AgentAccessLost):
            await read_linked_accounts(context, {"member_ids": [101]})
        context.account_links.get_links_for_members.assert_not_called()

    def test_only_review_account_datasets_require_reviewer_access(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "links.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript("""
                    CREATE TABLE links (player_tag TEXT, discord_user_id INTEGER);
                    INSERT INTO links VALUES ('#P0', 101);
                    CREATE TABLE suggestions (player_tag TEXT);
                    CREATE TABLE ignored_tags (player_tag TEXT);
                """)
                connection.commit()
            sources = (replace(SOURCES[0], path=path),)
            result, access = run_query(sources, None, set(),
                                      "SELECT player_tag, discord_user_id FROM links.links")
            self.assertEqual(result["rows"], [{"player_tag": "#P0", "discord_user_id": 101}])
            self.assertFalse(access)
            restricted = {"suggestions", "ignored_tags"}
            for table, level, _ in SOURCES[0].tables:
                with self.subTest(table=table):
                    expected_access = "recruiter_or_core" if table in restricted else "none"
                    self.assertEqual(level, expected_access)
                    result, access = run_query(sources, None, set(), f"SELECT * FROM links.{table}")
                    if table in restricted:
                        self.assertEqual(result["required_access"], ["recruiter_or_core"])
                        self.assertIn("error", result)
                        self.assertFalse(access)
                        result, access = run_query(
                            sources, None, {"recruiter_or_core"}, f"SELECT * FROM links.{table}",
                        )
                        self.assertEqual(result["rows"], [])
                        self.assertEqual(access, {"recruiter_or_core"})
                    else:
                        self.assertFalse(access)
                    self.assertNotIn("error", result)
