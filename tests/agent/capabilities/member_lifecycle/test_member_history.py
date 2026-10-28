from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.capabilities.member_lifecycle.test_agent_member_lifecycle import AgentMemberLifecycleTests
from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.infrastructure.discord.message_search import DiscordSearchMessage, DiscordSearchPage, DiscordHistoryPage
from elbow_helper.agent.capabilities.member_lifecycle.history import read_member_history


def log(message_id, text, *, bot=True, embeds=()):
    return DiscordSearchMessage(message_id, OVERSEEING_TERRACE, 99 + message_id,
                                "Log bot", text, "2026-09-01T00:00:00+00:00",
                                embeds=embeds, author_is_bot=bot)


class MemberHistoryTests(AgentMemberLifecycleTests):
    def setUp(self):
        super().setUp()
        self.context.source_message.id = 1000
        self.observed.name = "synthetic-handle"
        self.observed.display_name = "Synthetic member"
        self.context.bot.fetch_user = AsyncMock(return_value=SimpleNamespace(name="former-handle", display_name="Former member"))
        self.search = SimpleNamespace(search_page=AsyncMock(), history_page=AsyncMock())
        object.__setattr__(self.context, "message_search", self.search)

    async def test_union_deduplicates_and_history_finds_embed_only_entries(self):
        joined = log(20, "<@7> joined the server")
        left = log(30, "", embeds=({"title": "Left Server", "fields": [
            {"name": "Member", "value": "Synthetic member (ID: 7)"}]},))
        self.search.search_page.return_value = DiscordSearchPage((joined,), 0, 25, 1, None, False, False)
        self.search.history_page.return_value = DiscordHistoryPage((left, joined, log(40, "<@17> joined"),
            log(50, "Synthetic member joined"), log(60, "<@7> joined", bot=False)), 1000, 1, 100, None, True)
        result = await read_member_history(self.context, {"member_id": 7})
        self.assertEqual([row["message_id"] for row in result["entries"]], [20, 30])
        self.assertEqual([row["event"] for row in result["entries"]], ["join", "leave"])
        self.assertTrue(result["search_is_exhaustive"])
        calls = self.search.search_page.await_args_list
        self.assertEqual(calls[0].kwargs["mentions"], (7,))
        self.assertEqual([call.kwargs["content"] for call in calls], ["", "synthetic-handle", "Synthetic member"])
        self.assertEqual(result["entries"][0]["source"], f"https://discord.com/channels/1/{OVERSEEING_TERRACE}/20")

    async def test_departed_member_and_unavailable_index_still_use_history(self):
        self.search.search_page.side_effect = RuntimeError("unavailable")
        # Search errors are explicitly recognized; transport failure leaves honest coverage.
        from elbow_helper.infrastructure.discord.message_search import DiscordMessageSearchError
        self.search.search_page.side_effect = DiscordMessageSearchError("indexing")
        self.search.history_page.return_value = DiscordHistoryPage((log(10, "<@700> left"),), 1000, 1, 100, None, True)
        result = await read_member_history(self.context, {"member_id": 700})
        self.context.bot.fetch_user.assert_awaited_once_with(700)
        self.assertTrue(result["search_is_exhaustive"])
        self.assertEqual(len(result["entries"]), 1)

    async def test_hidden_source_is_never_queried(self):
        self.permissions[OVERSEEING_TERRACE] = False
        result = await read_member_history(self.context, {"member_id": 7})
        self.assertIn("error", result)
        self.search.search_page.assert_not_awaited()
        self.search.history_page.assert_not_awaited()

    async def test_partial_history_resumes_with_member_bound_cursor(self):
        self.search.search_page.return_value = DiscordSearchPage((log(10, "<@7> joined"),), 0, 25, 1, None, True, True)
        self.search.history_page.side_effect = [DiscordHistoryPage((log(20, "<@7> joined"),), 1000, 1, 100, 20, False), RuntimeError("unavailable")]
        first = await read_member_history(self.context, {"member_id": 7})
        self.assertFalse(first["search_is_exhaustive"])
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual([entry["message_id"] for entry in first["entries"]], [20])
        self.assertIn("error", await read_member_history(self.context, {"member_id": 8, "cursor": first["next_cursor"]}))
        self.search.history_page.side_effect = None
        self.search.history_page.return_value = DiscordHistoryPage((log(10, "<@7> joined"),), 20, 1, 100, None, True)
        final = await read_member_history(self.context, {"member_id": 7, "cursor": first["next_cursor"]})
        self.assertTrue(final["search_is_exhaustive"])
        self.assertEqual(final["entries"][0]["message_id"], 10)
