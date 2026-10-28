"""Synthetic source caches, commit invalidation and dated follow-up handoffs."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Event
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.infrastructure.discord.message_search import DiscordHistoryPage, DiscordSearchMessage
from elbow_helper.agent.conversation.context import compile_context, estimate_tokens
from elbow_helper.agent.conversation.findings import retained_finding, FINDINGS_INSTRUCTION
from elbow_helper.agent.conversation.findings import report_is_current
from elbow_helper.agent.cog import AgentCog
from elbow_helper.agent.conversation.preparation import ConversationContextMixin, prepare_history
from elbow_helper.agent.conversation.state import Conversation, ConversationRecord, ConversationTurn
from elbow_helper.agent.datasets.clash import read_clash
from elbow_helper.agent.engine.tool_call import evidence_record
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.families.war_reads import selected_wars
from elbow_helper.agent.families.identity_reads import clan_rosters
from elbow_helper.agent.families.cohort_activity import roster_cohort
from elbow_helper.agent.families.model_view import family_model_view
from elbow_helper.agent.read_cache import LIVE_READ_SECONDS, ReadCache, stored_read
from elbow_helper.agent.research.history import read_discord_channel_history
from elbow_helper.agent.research.history_cache import history_page
from elbow_helper.agent.research.history_cache import HISTORY_PAGE_SECONDS
from elbow_helper.agent.families import workflow_reads, identity_reads
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.bonus.config import BonusConfigRepository
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore
from elbow_helper.infrastructure.persistence.revisions import release_sqlite_revision
from agent.engine.test_agent_plan_flow import _context
from agent.test_compact_clash import read_context, synthetic_player, synthetic_clan
from features.test_ranked_stats import battle, observation, save_log, stamp
from elbow_helper.configuration.clans import CLANS


def context():
    player = synthetic_player()
    result = read_context({player['tag']: player})
    result.guild.name = 'Synthetic guild'
    result.member.display_name = 'Synthetic requester'
    result.source_message.created_at = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    return replace(result, read_cache=ReadCache())


class ReadCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_stored_fetch_delivers_the_same_ordinary_error_to_both_callers(self):
        started, release = Event(), Event()
        joined = asyncio.Event()
        class JoinedCache(ReadCache):
            callers = 0
            async def get(self, *args, **kwargs):
                self.callers += 1
                if self.callers == 2:
                    joined.set()
                return await super().get(*args, **kwargs)
        def broken(**kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError('Synthetic shared read was not released')
            raise sqlite3.OperationalError('Synthetic stored query failure')
        source = SimpleNamespace(cache_revision=lambda: 'one', window=Mock(side_effect=broken))
        local = replace(context(), ranked_queries=source, read_cache=JoinedCache())
        first = asyncio.create_task(stored_read(local, source, 'window'))
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        second = asyncio.create_task(stored_read(local, source, 'window'))
        try:
            await asyncio.wait_for(joined.wait(), 5)
        finally:
            release.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(error, sqlite3.OperationalError) for error in failures))
        source.window.assert_called_once()
        self.assertFalse(local.read_cache.pending)
        self.assertFalse(local.read_cache.active)

    async def test_cancelling_initiator_or_waiter_does_not_cancel_other_callers(self):
        for cancel_initiator in (True, False):
            with self.subTest(cancel_initiator=cancel_initiator):
                cache = ReadCache()
                started, release = asyncio.Event(), asyncio.Event()
                async def fetch():
                    started.set()
                    await release.wait()
                    return {'value': 'complete'}
                first = asyncio.create_task(cache.get('shared', fetch))
                await started.wait()
                second = asyncio.create_task(cache.get('shared', fetch))
                await asyncio.sleep(0)
                cancelled, surviving = (first, second) if cancel_initiator else (second, first)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                release.set()
                self.assertEqual((await surviving)[0], {'value': 'complete'})
                self.assertFalse(cache.pending)
                self.assertFalse(cache.active)

    async def test_generation_guards_survive_eviction_expiry_oversize_and_failed_fresh_reads(self):
        for removal in ('eviction', 'expiry', 'oversize', 'failure'):
            with self.subTest(removal=removal):
                cache = ReadCache()
                started, release = asyncio.Event(), asyncio.Event()
                async def old():
                    started.set()
                    await release.wait()
                    return {'value': 'old'}
                first = asyncio.create_task(cache.get('shared', old))
                await started.wait()
                if removal == 'failure':
                    with self.assertRaises(ValueError):
                        await cache.get('shared', AsyncMock(side_effect=ValueError('Synthetic')), fresh=True)
                else:
                    await cache.get('shared', AsyncMock(return_value={'value': 'new'}), fresh=True)
                    if removal == 'eviction':
                        with patch('elbow_helper.agent.read_cache.MAX_ENTRIES', 1):
                            cache.put('unrelated', {'value': 1})
                    elif removal == 'expiry':
                        cache.peek('shared', max_age=0)
                    else:
                        with patch('elbow_helper.agent.read_cache.MAX_BYTES', 1):
                            cache.put('shared', {'value': 'too large'})
                self.assertIn('shared', cache.versions)
                release.set()
                await first
                self.assertNotIn('shared', cache.entries)
                self.assertNotIn('shared', cache.versions)
                self.assertFalse(cache.active)

    async def test_real_roster_and_nested_cohort_keep_the_original_cached_observation_time(self):
        clan = synthetic_clan()
        clan['tag'] = CLANS['BEH'].tag
        local = replace(read_context({clan['tag']: clan}), read_cache=ReadCache())
        observed = datetime(2026, 10, 27, 11, 50, tzinfo=timezone.utc)
        args = {'clan_codes': ['BEH'], 'basis': 'current_in_game'}
        with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()), \
             patch('elbow_helper.agent.read_cache.datetime') as clock:
            clock.now.return_value = observed
            first = await clan_rosters(local, args)
            clock.now.return_value = observed + timedelta(minutes=10)
            reused = await clan_rosters(local, args)
            cohort = await roster_cohort(local, {**args, 'basis': 'clan_rosters', 'roster_basis': 'current_in_game'})
        local.bot.clash_client.get.assert_awaited_once()
        self.assertEqual(reused['clans'][0]['observed_at'], observed.isoformat())
        self.assertEqual(reused['rows'][0]['observed_at'], first['rows'][0]['observed_at'])
        writer = family_model_view({'family': 'read_member_cohort', **cohort})
        self.assertEqual(writer['rows'][0]['accounts'][0]['observed_at'], observed.isoformat())

    async def test_live_families_hit_normalized_tags_and_expire_at_fifteen_minutes(self):
        for kind in ('clan', 'player', 'cwl_group', 'war_log', 'capital_raids'):
            with self.subTest(kind=kind):
                local = context()
                tag = synthetic_player()['tag']
                clock = [0]
                with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()), \
                     patch('elbow_helper.agent.read_cache.monotonic', side_effect=lambda: clock[0]):
                    first = await read_clash(local, {'kind': kind, 'tags': [tag.lower()]})
                    other = replace(local, state=type(local.state)())
                    clock[0] = LIVE_READ_SECONDS - 1
                    reused = await read_clash(other, {'kind': kind, 'tags': [tag], 'detail': 'full'})
                    self.assertEqual(local.bot.clash_client.get.await_count, 1)
                    self.assertTrue(reused['items'][0]['reused'])
                    self.assertEqual(first['observed_at'], reused['observed_at'])
                    clock[0] = LIVE_READ_SECONDS
                    await read_clash(other, {'kind': kind, 'tags': [tag]})
                    self.assertEqual(local.bot.clash_client.get.await_count, 2)

    async def test_wars_are_never_cached_and_forced_fresh_reads_reach_nested_sources(self):
        local = context()
        tag = synthetic_player()['tag']
        with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()):
            for kind in ('current_war', 'cwl_war', 'battle_log'):
                args = {'kind': kind, 'tags': [tag]}
                await read_clash(local, args)
                await read_clash(local, args)
            self.assertEqual(local.bot.clash_client.get.await_count, 6)
            self.assertFalse(local.read_cache.entries)
            args = {'kind': 'player', 'tags': [tag]}
            await read_clash(local, args)
            await read_clash(local, {**args, 'max_age_seconds': 0})
            self.assertTrue(local.bot.clash_client.get.await_args.kwargs['fresh'])
            async def reader(nested, selected):
                raw = await read_clash(nested, args)
                return {'rows': raw['items']}
            tool = family_tool('read_member_accounts', 'Synthetic nested profile', {}, reader)
            with patch('elbow_helper.agent.families.core.require_evidence_access', AsyncMock()):
                await tool.handler(local, {'fresh': True})
            self.assertTrue(local.bot.clash_client.get.await_args.kwargs['fresh'])
            self.assertEqual(local.bot.clash_client.get.await_count, 9)

    async def test_shared_hits_recheck_access_and_isolate_asker_destination_and_private_scope(self):
        local = context()
        tag = synthetic_player()['tag']
        args = {'kind': 'player', 'tags': [tag]}
        with patch('elbow_helper.agent.datasets.clash.require_evidence_access', AsyncMock()) as check:
            await read_clash(local, args)
            await read_clash(replace(local, state=type(local.state)()), args)
            self.assertEqual(check.await_count, 4)
            self.assertEqual(local.bot.clash_client.get.await_count, 1)
            check.side_effect = RuntimeError('synthetic revoked access')
            with self.assertRaises(RuntimeError):
                await read_clash(local, args)
            check.side_effect = None
            for other in (replace(local, member=SimpleNamespace(id=999)),
                          replace(local, delivery_channel=SimpleNamespace(id=888)),
                          replace(local, private_pass=True)):
                await read_clash(other, args)
            self.assertEqual(local.bot.clash_client.get.await_count, 4)

    async def test_bounds_copies_failures_and_inflight_reads(self):
        cache = ReadCache()
        fetch = AsyncMock(return_value={'rows': [{'value': 1}]})
        values = await asyncio.gather(*(cache.get('same', fetch) for _ in range(4)))
        fetch.assert_awaited_once()
        values[0][0]['rows'][0]['value'] = 9
        self.assertEqual((await cache.get('same', fetch))[0]['rows'][0]['value'], 1)
        bad = AsyncMock(return_value={'error': 'synthetic failure'})
        await cache.get('bad', bad)
        await cache.get('bad', bad)
        self.assertEqual(bad.await_count, 2)
        with patch('elbow_helper.agent.read_cache.MAX_ENTRIES', 2), \
             patch('elbow_helper.agent.read_cache.MAX_BYTES', 100):
            cache.put('a', {'value': 1})
            cache.put('b', {'value': 2})
            cache.put('c', {'value': 3})
            self.assertLessEqual(len(cache.entries), 2)
            self.assertLessEqual(cache.bytes, 100)
            cache.put('large', {'value': 'x' * 101})
            self.assertNotIn('large', cache.entries)

    async def test_forced_fresh_result_survives_an_older_inflight_response(self):
        cache = ReadCache()
        started, release = asyncio.Event(), asyncio.Event()
        async def old():
            started.set()
            await release.wait()
            return {'value': 'older'}
        pending = asyncio.create_task(cache.get('same', old))
        await started.wait()
        fresh = AsyncMock(return_value={'value': 'newer'})
        await cache.get('same', fresh, fresh=True)
        release.set()
        await pending
        self.assertEqual((await cache.get('same', fresh))[0], {'value': 'newer'})
        fresh.assert_awaited_once()


class StoredCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_bonus_settings_changes_invalidate_without_a_database_write(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source.db'
            settings = Path(directory) / 'settings.json'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE synthetic (value INTEGER)')
            try:
                settings.write_text('{"revision":1}', encoding='utf-8')
                source = CwlQueries(SimpleNamespace(path=path), lambda: {}, bonus_config=BonusConfigRepository())
                source.bonus_wars = Mock(return_value={'rows': []})
                local = replace(_context(), cwl_queries=source, read_cache=ReadCache())
                with patch('elbow_helper.features.cwl.bonus.config.BONUS_CONFIG_FILE', settings):
                    args = dict(clan_code='BEH', war_ids=['synthetic'])
                    await stored_read(local, source, 'bonus_wars', **args)
                    await stored_read(local, source, 'bonus_wars', **args)
                    source.bonus_wars.assert_called_once()
                    settings.write_text('{"revision":2}', encoding='utf-8')
                    await stored_read(local, source, 'bonus_wars', **args)
                    self.assertEqual(source.bonus_wars.call_count, 2)
            finally:
                release_sqlite_revision(path)

    async def test_finished_wars_and_cwl_survive_time_but_invalidate_on_insert_and_correction(self):
        with TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / 'wars.db')
            repository.initialize()
            try:
                source = CwlQueries(repository, lambda: {})
                source.attendance = Mock(wraps=source.attendance)
                local = replace(_context(), cwl_queries=source, read_cache=ReadCache())
                for kind in ('REG', 'CWL'):
                    args = {'clan_codes': ['BEH'], 'latest_completed': 1}
                    first = await selected_wars(local, args, kind)
                    with patch('elbow_helper.agent.read_cache.monotonic', return_value=10**10):
                        self.assertEqual(await selected_wars(local, args, kind), first)
                    self.assertEqual(source.attendance.call_count, 1 if kind == 'REG' else 4)
                    with closing(sqlite3.connect(repository.path)) as connection, connection:
                        connection.execute('''INSERT INTO wars(war_id,war_type,clan_code,clan_tag,
                            opponent_tag,state,preparation_start_ts,start_ts,end_ts,last_seen_ts,
                            team_size,attacks_per_member) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (kind, kind, 'BEH', '#P0', '#Q0', 'warEnded', 1, 2, 100, 100, 1, 2))
                    await selected_wars(local, args, kind)
                    with closing(sqlite3.connect(repository.path)) as connection, connection:
                        connection.execute('UPDATE wars SET last_seen_ts=101 WHERE war_id=?', (kind,))
                    await selected_wars(local, args, kind)
                self.assertEqual(source.attendance.call_count, 6)
                source.performance = Mock(wraps=source.performance)
                first = await stored_read(local, source, 'performance', season='2026-09')
                self.assertEqual(await stored_read(local, source, 'performance', season='2026-09'), first)
                source.performance.assert_called_once()
            finally:
                release_sqlite_revision(repository.path)

    async def test_stored_ranked_window_is_invalidated_by_new_battles_and_backfill(self):
        with TemporaryDirectory() as directory:
            store = RankedStore(Path(directory) / 'ranked.db')
            store.initialize()
            try:
                save_log(store, [battle()])
                source = RankedQueries(store)
                source.window = Mock(wraps=source.window)
                local = replace(_context(), ranked_queries=source, read_cache=ReadCache())
                args = dict(after=stamp(), before=stamp(4), selector='account_tags', values=('#P0',))
                first = await stored_read(local, source, 'window', **args)
                self.assertEqual(first['accounts'][0]['attacks_used'], 1)
                self.assertEqual(await stored_read(local, source, 'window', **args), first)
                source.window.assert_called_once()
                save_log(store, [battle(minute=2)], observation(minute=5))
                updated = await stored_read(local, source, 'window', **args)
                self.assertEqual(updated['accounts'][0]['attacks_used'], 2)
                save_log(store, [battle(stars=3)], observation(minute=6))
                await stored_read(local, source, 'window', **args)
                self.assertEqual(source.window.call_count, 3)
                await stored_read(replace(local, fresh_reads=True), source, 'window', **args)
                self.assertEqual(source.window.call_count, 4)
            finally:
                release_sqlite_revision(store.path)


def message(identifier, channel=4):
    return DiscordSearchMessage(identifier, channel, 2, 'Synthetic author', 'Unchanged member prose',
                                '2026-10-27T11:00:00+00:00')


class HistoryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_head_checks_and_cursor_hits_do_not_extend_page_lifetime(self):
        local = context()
        original = DiscordHistoryPage((message(90), message(80)), 100, 0, 2, 80, False)
        edited = replace(message(90), content='Synthetic edited message')
        source = SimpleNamespace(history_page=AsyncMock(side_effect=[original,
            DiscordHistoryPage((), 110, 90, 2, None, True),
            DiscordHistoryPage((edited,), 120, 0, 2, None, True)]))
        local = replace(local, message_search=source)
        clock = [0]
        args = dict(channel_id=4, after_id=0, limit=2)
        with patch('elbow_helper.agent.read_cache.monotonic', side_effect=lambda: clock[0]):
            await history_page(local, before_id=100, **args)
            clock[0] = 100
            await history_page(local, before_id=110, **args)
            clock[0] = HISTORY_PAGE_SECONDS - 1
            await history_page(local, before_id=110, refresh_head=False, **args)
            self.assertEqual(source.history_page.await_count, 2)
            clock[0] = HISTORY_PAGE_SECONDS
            current = await history_page(local, before_id=120, **args)
        source.history_page.assert_awaited_with(channel_id=4, before_id=120, after_id=0, limit=2)
        self.assertEqual([row.message_id for row in current.messages], [90])
        self.assertEqual(current.messages[0].content, 'Synthetic edited message')

    async def test_raw_edit_delete_and_bulk_delete_invalidate_all_scopes_for_the_channel(self):
        for event in ('on_raw_message_edit', 'on_raw_message_delete', 'on_raw_bulk_message_delete'):
            with self.subTest(event=event):
                local = context()
                original = DiscordHistoryPage((message(90),), 100, 0, 2, None, True)
                updated = replace(original, messages=())
                source = SimpleNamespace(history_page=AsyncMock(side_effect=[original, original, updated]))
                local = replace(local, message_search=source)
                args = dict(channel_id=4, before_id=100, after_id=0, limit=2, refresh_head=False)
                await history_page(local, **args)
                await history_page(replace(local, member=SimpleNamespace(id=999)), **args)
                other_key = ('history', (1, 2, 4), id(source), 5, 0, 2, 100)
                local.read_cache.put(other_key, replace(original, messages=(message(90, channel=5),)))
                cog = SimpleNamespace(read_cache=local.read_cache)
                await getattr(AgentCog, event)(cog, SimpleNamespace(channel_id=4, message_id=90, message_ids={90}))
                self.assertEqual(set(local.read_cache.entries), {other_key})
                self.assertFalse((await history_page(local, **args)).messages)
                self.assertEqual(source.history_page.await_count, 3)

    async def test_edit_during_head_fetch_cannot_restore_the_invalidated_page(self):
        local = context()
        started, release = asyncio.Event(), asyncio.Event()
        original = DiscordHistoryPage((message(90),), 100, 0, 2, None, True)
        async def fetch(**args):
            if args['before_id'] == 100:
                return original
            if args['after_id'] == 90:
                started.set()
                await release.wait()
                return DiscordHistoryPage((), 110, 90, 2, None, True)
            return DiscordHistoryPage((), 110, 0, 2, None, True)
        local = replace(local, message_search=SimpleNamespace(history_page=AsyncMock(side_effect=fetch)))
        args = dict(channel_id=4, after_id=0, limit=2)
        await history_page(local, before_id=100, **args)
        pending = asyncio.create_task(history_page(local, before_id=110, **args))
        await started.wait()
        local.read_cache.invalidate_history(4)
        release.set()
        self.assertFalse((await pending).messages)
        self.assertFalse(local.read_cache.entries)
        self.assertFalse(local.read_cache.active)

    async def test_new_head_is_fetched_above_newest_cached_and_older_pages_are_reused(self):
        local = context()
        pages = [DiscordHistoryPage((message(90), message(80)), 100, 0, 2, 80, False),
                 DiscordHistoryPage((), 110, 90, 2, None, True),
                 DiscordHistoryPage((message(105),), 120, 90, 2, None, True)]
        source = SimpleNamespace(history_page=AsyncMock(side_effect=pages))
        local = replace(local, message_search=source)
        args = dict(channel_id=4, after_id=0, limit=2)
        first = await history_page(local, before_id=100, **args)
        hit = await history_page(local, before_id=100, refresh_head=False, **args)
        self.assertEqual(first, hit)
        self.assertEqual(source.history_page.await_count, 1)
        same = await history_page(local, before_id=110, **args)
        self.assertEqual([row.message_id for row in same.messages], [90, 80])
        source.history_page.assert_awaited_with(channel_id=4, before_id=110, after_id=90, limit=2)
        updated = await history_page(local, before_id=120, **args)
        self.assertEqual([row.message_id for row in updated.messages], [105, 90])
        self.assertEqual(updated.next_before_id, 90)

    async def test_history_pages_do_not_cross_requester_or_destination_scopes(self):
        local = context()
        source = SimpleNamespace(history_page=AsyncMock(return_value=DiscordHistoryPage(
            (message(90),), 100, 0, 2, None, True)))
        local = replace(local, message_search=source)
        args = dict(channel_id=4, before_id=100, after_id=0, limit=2, refresh_head=False)
        await history_page(local, **args)
        await history_page(local, **args)
        self.assertEqual(source.history_page.await_count, 1)
        await history_page(replace(local, member=SimpleNamespace(id=999)), **args)
        await history_page(replace(local, delivery_channel=SimpleNamespace(id=888)), **args)
        self.assertEqual(source.history_page.await_count, 3)

    async def test_denied_page_hit_rechecks_source_and_fresh_page_bypasses_cache(self):
        local = context()
        async def fetch(**arguments):
            return DiscordHistoryPage((message(arguments['before_id'] - 10),),
                arguments['before_id'], arguments['after_id'], arguments['limit'], None, True)
        source = SimpleNamespace(history_page=AsyncMock(side_effect=fetch))
        local = replace(local, message_search=source)
        access = 'elbow_helper.agent.research.history.accessible_message_channel'
        with patch(access, AsyncMock(return_value=local.source_message.channel)) as checked:
            await read_discord_channel_history(local, {'channel_id': 4})
            checked.side_effect = [local.source_message.channel, None]
            denied = await read_discord_channel_history(local, {'channel_id': 4})
            self.assertIn('error', denied)
            self.assertEqual(checked.await_count, 4)
        calls = source.history_page.await_count
        await history_page(replace(local, fresh_reads=True), channel_id=4, before_id=100, after_id=0, limit=2)
        self.assertEqual(source.history_page.await_count, calls + 1)


class FollowupFindingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_enrichments_keep_transitive_war_revisions(self):
        for family, arguments in (
            ('read_recruitment_pipeline', {'stage': 'active_trials', 'performance': {'war_type': 'REG', 'latest_completed': 1}}),
            ('read_promotion_cases', {'performance': {'war_type': 'REG', 'latest_completed': 1}}),
            ('read_hibernation', {'include_activity': True, 'activity_kinds': ['regular_war']}),
        ):
            with self.subTest(family=family):
                revision = ['one']
                wars = {'rows': [{'player_tag': '#P0', 'war_type': 'REG', 'war_id': 'synthetic',
                    'start_ts': 2, 'end_ts': 10, 'attacks_used': 1, 'final_capture': True}], 'war_ids': ['synthetic']}
                source = SimpleNamespace(cache_revision=lambda: revision[0], attendance=Mock(return_value=wars))
                local = replace(context(), cwl_queries=source,
                    account_links=SimpleNamespace(get_links_for_members=lambda ids: {2: [{'player_tag': '#P0'}]}),
                    examination_queries=SimpleNamespace(case_registrations=lambda: [SimpleNamespace(ticket_channel_id=4)],
                        case_snapshot=lambda **kw: SimpleNamespace(cases=[SimpleNamespace(
                            applicant_member_id=2, workflow_status='open', ticket_channel_id=4)])))
                async def performance(nested, rows, selection):
                    result = await stored_read(nested, nested.cwl_queries, 'attendance', **selection)
                    for row in rows:
                        row['war_attendance'] = result['rows']
                    return {'coverage': 'Synthetic nested war read'}
                tool = next(item for item in workflow_reads.TOOLS if item.definition.name == family)
                with patch('elbow_helper.agent.families.core.require_evidence_access', AsyncMock()), \
                     patch.object(workflow_reads, 'has_lookup_access', return_value=True), \
                     patch.object(workflow_reads, 'active_trial_rows', AsyncMock(return_value={'rows': [{'applicant_member_id': 2}]})), \
                     patch.object(workflow_reads, 'complete_snapshot', AsyncMock(return_value={'rows': [{'member_id': 2, 'started_ts': 1}]})), \
                     patch.object(workflow_reads, 'member_fields', side_effect=lambda member, identifier: {'member_id': identifier}), \
                     patch.object(workflow_reads, 'asdict', side_effect=vars), \
                     patch.object(workflow_reads, 'performance_projection', side_effect=performance):
                    result = await tool.handler(local, arguments)
                report = local.state.reports[result['report_id']]
                self.assertEqual(result['source_versions'], {'cwl_queries': 'one'})
                self.assertTrue(report_is_current(local, report))
                revision[0] = 'corrected'
                self.assertFalse(report_is_current(local, report))

    async def test_activity_continuations_preserve_revisions_and_fresh_ref_rereads_every_source(self):
        revision = ['one']
        def attendance(**kw):
            return {'rows': [{'player_tag': '#P0', 'player_name': 'Synthetic', 'war_id': 'war',
                'clan_code': 'BES', 'attacks_used': 1 if revision[0] == 'one' else 2,
                'attacks_expected': 2, 'missed': 1 if revision[0] == 'one' else 0}], 'coverage': {}}
        source = SimpleNamespace(cache_revision=lambda: revision[0], attendance=Mock(side_effect=attendance))
        local = replace(_context(), cwl_queries=source, read_cache=ReadCache(),
            account_links=SimpleNamespace(get_links_by_tags=lambda tags: {'#P0': {'discord_user_id': 42}}))
        owner = SimpleNamespace(id=42, display_name='Synthetic owner', roles=[], joined_at=None)
        local.guild.get_member = lambda identifier: owner if identifier == 42 else local.member
        roster = {'rows': [{'clan_code': 'BES', 'player_tag': '#P0', 'player_name': 'Synthetic', 'donations': 1}],
                  'count_complete': True, 'clans': []}
        tool = next(item for item in identity_reads.TOOLS if item.definition.name == 'read_member_cohort')
        selection = {'basis': 'clan_rosters', 'include_activity': True, 'after': '2025-12-01T00:00:00+00:00'}
        from elbow_helper.agent.families import cohort_activity
        with patch('elbow_helper.agent.families.core.require_evidence_access', AsyncMock()), \
             patch.object(identity_reads, 'clan_rosters', AsyncMock(return_value=roster)) as rosters, \
             patch.object(cohort_activity, 'searchable_channel_ids', AsyncMock(return_value=())), \
             patch.object(cohort_activity, 'chat_projection', wraps=cohort_activity.chat_projection) as chat:
            first = await tool.handler(local, selection)
            revision[0] = 'corrected'
            second = await tool.handler(local, {'basis': 'clan_rosters', 'activity_ref': first['report_id']})
            third = await tool.handler(local, {'basis': 'clan_rosters', 'activity_ref': second['report_id']})
            self.assertEqual(second['source_versions'], first['source_versions'])
            self.assertEqual(third['source_versions'], first['source_versions'])
            self.assertEqual(second['observed_at'], first['observed_at'])
            self.assertEqual(third['resolved_inputs'], selection)
            self.assertFalse(report_is_current(local, local.state.reports[third['report_id']]))
            source.attendance.assert_called_once()
            rosters.assert_awaited_once()
            fresh = await tool.handler(local, {'basis': 'clan_rosters', 'activity_ref': third['report_id'], 'fresh': True})
            self.assertEqual(fresh['source_versions'], {'cwl_queries': 'corrected'})
            self.assertEqual(fresh['rows'][0]['attacks_used'], 2)
            self.assertEqual(source.attendance.call_count, 2)
            self.assertEqual(rosters.await_count, 2)
            self.assertTrue(rosters.call_args.args[0].fresh_reads)
            self.assertNotIn('progress', chat.call_args.kwargs)
            self.assertEqual(chat.call_count, 4)

    async def make_finding(self, family='read_clan_rosters', source=None):
        local = context()
        if source is not None:
            local = replace(local, cwl_queries=source)
        observed = local.source_message.created_at - timedelta(minutes=10)
        reader = AsyncMock(return_value={'rows': [
            {'player_tag': '#P0', 'clan_code': 'BEH', 'observed_at': observed.isoformat(),
             'source_detail': 'synthetic unique retained detail ' + str(index) + 'x' * 300}
            for index in range(450)]})
        tool = family_tool(family, 'Synthetic roster', {}, reader)
        with patch('elbow_helper.agent.families.core.require_evidence_access', AsyncMock()):
            result = await tool.handler(local, {'view': 'count'})
        full = local.state.reports[result['report_id']].page(limit=450)
        plan = {'goal': 'Assess synthetic roster', 'effort': 'high', 'output': 'text',
                'steps': [{'id': 'roster', 'capability': family, 'arguments': {}, 'depends_on': []}]}
        encoded = evidence_record(call_id='roster', tool=family, arguments={}, result=json.dumps(full),
            result_complete=True, raw_result_characters=len(json.dumps(full)), context=local, plan=plan)
        record = ConversationRecord(request_message_id=3, member_id=local.member.id,
            created_at=local.source_message.created_at.isoformat(), question='Assess this roster',
            generated_answer='Synthetic finding: 450 accounts.', delivered_answer='Synthetic finding: 450 accounts.',
            local_context='', evidence=(encoded,), report_ids=(result['report_id'],), reply_ids=(5,),
            delivery_complete=True)
        turn = ConversationTurn('{}', frozenset({4}), record=record)
        return local, tool, reader, encoded, turn, observed

    async def test_followup_carries_plan_answer_handles_and_actual_as_of_times_without_full_rows(self):
        local, tool, reader, encoded, turn, observed = await self.make_finding()
        local.state.authorized_history = (turn,)
        compiled = compile_context(question='Now as a sheet', local_context='', context=local, tools=())
        self.assertIn('Synthetic finding: 450 accounts.', compiled.prompt)
        self.assertNotIn('synthetic unique retained detail', compiled.prompt)
        prior = json.loads(compiled.answer_context)['prior_findings'][0]
        finding = json.loads(prior['result'])
        self.assertEqual(prior['plan']['goal'], 'Assess synthetic roster')
        self.assertIn(observed.isoformat(), finding['observation_times'])
        self.assertTrue(finding['dated_live_figures_required'])
        self.assertIn('say "as of <time>"', FINDINGS_INSTRUCTION)
        self.assertIn(observed.isoformat(), compiled.answer_context)
        self.assertLess(estimate_tokens(json.dumps(prior)), estimate_tokens(encoded) * .05)
        with patch('elbow_helper.agent.families.core.require_evidence_access', AsyncMock()), \
             patch('elbow_helper.agent.families.core.authorize_report', AsyncMock(return_value=True)):
            exported = await tool.handler(local, {**finding['read_arguments'], 'view': 'count', 'delivery': 'csv'})
        self.assertEqual(exported['artifact']['total_rows'], 450)
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report', AsyncMock(return_value=True)):
            writer = await cohort_writer_evidence(local, {'export': exported})
        self.assertFalse(writer['export']['rows'])
        self.assertEqual(writer['export']['artifact']['total_rows'], 450)
        reader.assert_awaited_once()

    async def test_replied_turn_keeps_findings_when_mandatory_prompt_exceeds_soft_target(self):
        local, _, _, _, turn, _ = await self.make_finding()
        local.state.authorized_history = (turn,)
        local.state.reply_context = {'conversation': {'request_message_id': turn.record.request_message_id}}
        compiled = compile_context(question='BEM only', local_context='', context=local,
                                   tools=(), target_tokens=100)
        self.assertTrue(compiled.exceeds_target)
        self.assertTrue(json.loads(compiled.answer_context)['prior_findings'])
        self.assertIn('Assess synthetic roster', compiled.prompt)

    async def test_superseded_storage_and_cross_asker_reports_have_no_reusable_handle(self):
        revision = ['one']
        source = SimpleNamespace(cache_revision=lambda: revision[0])
        local, _, _, encoded, _, _ = await self.make_finding('read_war_attendance', source)
        self.assertIn('report_id', json.loads(json.loads(retained_finding(local, encoded))['result']))
        revision[0] = 'two'
        for changed in (local, replace(local, member=SimpleNamespace(id=999))):
            finding = json.loads(json.loads(retained_finding(changed, encoded))['result'])
            self.assertNotIn('report_id', finding)
            self.assertEqual(finding['status'], 'superseded_or_unavailable')

    async def test_history_and_report_permissions_are_rechecked_before_followup_reuse(self):
        local, _, _, _, turn, _ = await self.make_finding()
        conversation = Conversation(local.guild.id, 4)
        conversation.reports.update(local.state.reports)
        conversation.report_sources.update(local.state.report_sources)
        conversation.report_access_requirements.update(local.state.report_access_requirements)
        local = replace(local, state=type(local.state)())
        with patch('elbow_helper.agent.conversation.preparation.request_scope', AsyncMock()), \
             patch('elbow_helper.agent.conversation.preparation.accessible_message_channel',
                   AsyncMock(return_value=None)):
            await prepare_history(local, (turn,))
            await ConversationContextMixin()._load_authorized_reports(conversation, local)
        self.assertEqual(local.state.authorized_history, ())
        self.assertFalse(local.state.reports)
        self.assertTrue(local.state.preserved_reports)
