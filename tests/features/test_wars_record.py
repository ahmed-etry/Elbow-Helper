"""War boards, summaries, roles and queries from the family record.

Synthetic wars are written through the collectors' own storage path, then the
record-driven features are checked against what the same Clash payloads
render. No Discord or Clash request is made.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS
from elbow_helper.configuration.clans import CLAN_WAR_ROLE_IDS
from elbow_helper.data.reads import WarReads
from elbow_helper.data.store.war_projection import HOUR
from elbow_helper.features.wars import config
from elbow_helper.features.wars.board import WarBoardMixin
from elbow_helper.features.wars.cog import WarManager
from elbow_helper.features.wars.helpers import HelperMixin, build_war_id
from elbow_helper.features.wars.queries import WarQueries
from elbow_helper.features.wars.record import RecordMixin
from elbow_helper.infrastructure.discord.war_boards import build_war_summary_embed
from elbow_helper.features.wars.roles import WarRoleMixin
from elbow_helper.features.wars.state import StateMixin
from elbow_helper.features.wars.tasks import TaskMixin
from elbow_helper.features.wars.warflow import WarflowMixin
from elbow_helper.infrastructure.discord.publishing import PublishingQueue

from tests.data.test_wars import END, SECOND, START, WarFixture, war
from tests.features.test_wars import _war_emojis

DAY = 24 * HOUR
CLAN = {"Hellbow": "#PY"}
LEADERSHIP = 7
LINKS = {"#P0": 42, "#P2": 43}


class _RecordManager(RecordMixin, HelperMixin, WarBoardMixin, WarRoleMixin, WarflowMixin, StateMixin, TaskMixin):
    pass


async def _history(messages):
    for message in messages:
        yield message


def later_war(state="preparation"):
    """The clan's next war, against another opponent, two days later."""
    payload = war(state, clans=("#PY", "#CU"), end="20261030T180000.000Z")
    payload.update(preparationStartTime="20261028T180000.000Z", startTime="20261029T180000.000Z")
    return payload


def as_rendered(embed):
    return (embed.title, embed.url, embed.description, embed.color, embed.thumbnail.url,
            embed.author.name, [(field.name, field.value) for field in embed.fields])


class RecordWiringTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = self.at
        for patcher in (patch.object(config, "CLAN_TAGS", CLAN),
                        patch("elbow_helper.data.reads.core.now_us", side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = self.build_manager()

    def build_manager(self, *, processed=(), links=LINKS):
        manager = _RecordManager()
        manager.war_reads = WarReads(self.store)
        manager.record_boards, manager._record_checked, manager._record_views = {}, {}, set()
        manager._record_lock = asyncio.Lock()
        manager.war_observations = {}
        manager.war_board_registry = {}
        manager.cache = {}
        manager._save_cache_async = AsyncMock()
        manager.war_emojis = MagicMock(get=AsyncMock(return_value=_war_emojis()))
        manager.war_role_lineups, manager.war_role_managed_members = {}, {}
        manager._war_role_locks, manager._war_role_missing_links = {}, {}
        manager._roster_role_claim = AsyncMock(return_value=False)
        manager.account_links = MagicMock()
        manager.account_links.get_all_links.return_value = {tag: {"discord_user_id": member} for tag, member in links.items()}
        manager.summary_registry = {}
        manager.processed_war_order = list(processed)
        manager.processed_war_ids = set(processed)
        manager.war_context, manager._war_state_locks, manager._wars_in_flight = {}, {}, {}
        manager._war_summary_state_lock = asyncio.Lock()
        manager.clan_channels = {"Hellbow": {"leadership_channel": LEADERSHIP, "leadership_role": 8}}
        manager._startup_sync_done = asyncio.Event()

        self.role = MagicMock(id=CLAN_WAR_ROLE_IDS["BEH"])
        self.members = {}
        for member_id in (42, 43):
            member = MagicMock(roles=[])
            member.add_roles = AsyncMock(side_effect=lambda role, reason, member=member: member.roles.append(role))
            member.remove_roles = AsyncMock(side_effect=lambda role, reason, member=member: member.roles.remove(role))
            self.members[member_id] = member
        guild = MagicMock()
        guild.get_role.return_value = self.role
        guild.get_member.side_effect = self.members.get

        self.board = MagicMock(id=CLAN_WAR_CHANNELS["BEH"])
        self.posted = MagicMock(id=900, channel=self.board, components=[], embeds=[])
        self.posted.edit = AsyncMock()
        self.board.send = AsyncMock(side_effect=self.post_board)
        self.board.fetch_message = AsyncMock(return_value=self.posted)
        self.leadership = MagicMock(id=LEADERSHIP)
        self.summary = MagicMock(id=901, channel=self.leadership, created_at=datetime(2026, 10, 28, 18, 2, tzinfo=timezone.utc))
        self.summary.author.id = 99
        self.leadership.send = AsyncMock(return_value=self.summary)
        self.leadership.history.side_effect = lambda **kwargs: _history([])
        self.leadership.fetch_message = AsyncMock()

        manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))
        manager.bot.user.id = 99
        manager.bot.wait_until_ready = AsyncMock()
        manager.bot.get_guild.return_value = guild
        manager.bot.get_channel.side_effect = {self.board.id: self.board, LEADERSHIP: self.leadership}.get
        return manager

    async def post_board(self, *, embed, view):
        self.posted.embeds = [embed]
        self.posted.components = [MagicMock(children=[MagicMock(custom_id=item.custom_id) for item in view.children])] if view else []
        return self.posted

    async def check(self, payload, at, **kwargs):
        await self.record(payload, at=at, **kwargs)
        self.now = at + 5 * SECOND


    async def test_ended_war_posts_one_summary_with_the_record_misses_and_a_restart_posts_none(self):
        ended = war("warEnded")
        await self.check(ended, END + 90 * SECOND)
        await self.manager._refresh_from_record()

        self.leadership.send.assert_awaited_once()
        summary = self.leadership.send.await_args.kwargs["embed"]
        expected = build_war_summary_embed(ended, _war_emojis(), timestamp=summary.timestamp)
        HelperMixin()._add_missed_attack_fields(expected, HelperMixin()._compute_missed(ended))
        self.assertEqual(as_rendered(summary), as_rendered(expected))
        self.assertEqual(summary.fields[0].value, "Player #P0 — 1 attack\nPlayer #P2 — 1 attack")
        self.assertEqual(summary.timestamp, datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(self.manager.processed_war_ids, {build_war_id(ended)})

        # A new check with nothing new posts nothing more.
        await self.check(ended, END + 400 * SECOND)
        await self.manager._refresh_from_record()
        self.leadership.send.assert_awaited_once()

        # A restart, whether the summary was posted by the old poll or the record, replays nothing.
        restarted = self.build_manager(processed=[build_war_id(ended)])
        await restarted._sync_war_state_from_record()
        self.leadership.send.assert_not_awaited()
        self.assertTrue(restarted._startup_sync_done.is_set())
        self.assertEqual(restarted.war_context["Hellbow"]["last_state"], "warended")

    async def test_restart_before_the_summary_was_saved_adopts_the_posted_message(self):
        ended = war("warEnded")
        await self.check(ended, END + 90 * SECOND)
        restarted = self.build_manager()
        posted = build_war_summary_embed(ended, _war_emojis(), timestamp=datetime(2026, 10, 28, 18, tzinfo=timezone.utc))
        self.summary.embeds = [posted]
        self.leadership.history.side_effect = lambda **kwargs: _history([self.summary])
        await restarted._sync_war_state_from_record()
        self.leadership.send.assert_not_awaited()
        self.assertIn(build_war_id(ended), restarted.processed_war_ids)

    async def test_incomplete_final_capture_names_misses_from_the_lineup_like_the_old_poll(self):
        ended = war("warEnded")
        # One account lost its map position: the lineup is incomplete, so the record leaves misses unknown.
        del ended["clan"]["members"][1]["mapPosition"]
        await self.check(war("inWar"), self.at)
        await self.check(ended, END + 90 * SECOND)
        current = await self.manager.war_reads.current("#PY", lineups=True)
        self.assertEqual(current["coverage"]["signals"][0]["status"], "partial")
        await self.manager._refresh_from_record()

        summary = self.leadership.send.await_args.kwargs["embed"]
        self.assertEqual(summary.fields[0].value, "\n".join(HelperMixin()._compute_missed(ended)))

    async def test_preparation_grants_roles_and_war_end_removes_them_including_alts(self):
        await self.check(war("preparation"), START - HOUR)
        await self.manager._refresh_from_record()
        self.assertEqual(self.manager.war_role_lineups, {"BEH": {"#P0": 42, "#P2": 43}})
        self.assertTrue(all(self.role in member.roles for member in self.members.values()))

        await self.check({"state": "notInWar"}, END + DAY)
        await self.manager._refresh_from_record()
        self.assertEqual(self.manager.war_role_lineups, {})
        self.assertFalse(any(self.role in member.roles for member in self.members.values()))

    async def test_one_member_with_both_lineup_accounts_holds_one_claim(self):
        await self.check(war("preparation"), START - HOUR)
        manager = self.build_manager(links={"#P0": 42, "#P2": 42})
        await manager._refresh_from_record()
        self.assertEqual(manager.war_role_lineups, {"BEH": {"#P0": 42, "#P2": 42}})
        self.assertEqual(manager.war_role_managed_members, {"BEH": {42}})
        self.members[42].add_roles.assert_awaited_once()
        self.members[43].add_roles.assert_not_awaited()

    async def test_partial_recorded_lineup_keeps_every_role(self):
        partial = war("inWar")
        partial["clan"]["members"].pop()
        await self.check(partial, self.at)
        self.manager.war_role_lineups = {"BEH": {"#P0": 42, "#P2": 43}}
        self.manager.war_role_managed_members = {"BEH": {42, 43}}
        for member in self.members.values():
            member.roles.append(self.role)
        with self.assertLogs("elbow_helper.features.wars.record", "WARNING"):
            await self.manager._refresh_from_record()
        self.assertEqual(self.manager.war_role_lineups, {"BEH": {"#P0": 42, "#P2": 43}})
        self.manager._roster_role_claim.assert_not_awaited()
        self.assertTrue(all(self.role in member.roles for member in self.members.values()))
        # The board still shows the war as Clash reported it, as the old poll did.
        self.board.send.assert_awaited_once()

    async def test_stale_or_failed_evidence_changes_nothing(self):
        await self.check(war("inWar"), self.at)
        await self.manager._refresh_from_record()
        granted = dict(self.manager.war_role_lineups)
        edits = self.board.send.await_count

        # The collector stopped checking: everything waits.
        self.now = self.at + HOUR
        await self.manager._refresh_from_record()
        self.assertEqual(self.manager.war_role_lineups, granted)
        self.assertNotIn("BEH", self.manager.war_observations)
        queries = WarQueries(self.manager)
        self.assertEqual(queries.status("BEH").evidence_status, "cached_unverified")

        # A failed check is not evidence of anything.
        await self.check(war("inWar"), self.at + 2 * HOUR, status=500)
        await self.manager._refresh_from_record()
        self.assertEqual(self.manager.war_role_lineups, granted)
        self.assertEqual(self.board.send.await_count, edits)
        self.manager.bot.publishing.edit_message.assert_not_called()
        self.leadership.send.assert_not_awaited()


    async def test_an_unchanged_check_is_not_processed_again_but_a_failed_refresh_retries(self):
        await self.check(war("inWar"), self.at)
        self.manager._update_war_board = AsyncMock(wraps=self.manager._update_war_board)
        await self.manager._refresh_from_record()
        await self.manager._refresh_from_record()
        self.manager._update_war_board.assert_awaited_once()

        self.manager._update_war_board.side_effect = OSError("disk full")
        await self.check(war("inWar"), self.at + 300 * SECOND)
        with self.assertLogs("elbow_helper.features.wars.record", "ERROR"):
            await self.manager._refresh_from_record()
        self.assertNotIn("Hellbow", self.manager._record_checked)
        self.manager._update_war_board.side_effect = None
        self.manager._update_war_board.reset_mock()
        await self.manager._refresh_from_record()
        self.manager._update_war_board.assert_awaited_once()


class ManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):

    def manager(self, bot, cache=None):
        with patch("elbow_helper.features.wars.cog.load_cache", return_value=cache or {}), \
                patch("elbow_helper.features.wars.cog.save_cache") as save:
            manager = WarManager(bot)
        self.saved = save
        return manager

    async def test_startup_replays_the_record_and_each_collection_pass_refreshes_it(self):
        bot = MagicMock(war_reads=MagicMock())
        manager = self.manager(bot)
        manager._periodic_summary_cleanup = AsyncMock()
        manager._sync_war_state_from_record = AsyncMock()
        manager._refresh_from_record = AsyncMock()
        await manager.on_ready()
        await manager.on_ready()
        await asyncio.sleep(0)
        manager._sync_war_state_from_record.assert_awaited_once()
        self.assertFalse(hasattr(manager, "_poll_task"))
        # A pass before the startup replay finishes is left to the replay.
        await manager.on_family_wars_collected()
        manager._refresh_from_record.assert_not_awaited()
        manager._startup_sync_done.set()
        await manager.on_family_wars_collected()
        manager._refresh_from_record.assert_awaited_once_with()
        manager.cog_unload()


    async def test_without_the_record_reads_a_refresh_changes_nothing(self):
        manager = self.manager(MagicMock(war_reads=None))
        manager._refresh_clan_from_record = AsyncMock()
        with self.assertLogs("elbow_helper.features.wars.record", "WARNING"):
            await manager._refresh_from_record()
        manager._refresh_clan_from_record.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
