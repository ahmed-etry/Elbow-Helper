"""Freshness uses delivered server history without changing the cached catalogue."""

from dataclasses import replace
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import discord

from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin, AgentDeliveryUnknown
from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.discord_actions.gifs import find_gif
from elbow_helper.agent.discord_actions.meme_usage import RETENTION_SECONDS, record_delivered_meme, select_meme
from elbow_helper.agent.conversation.state import Conversation, ConversationRecord, ConversationTurn
from elbow_helper.agent.engine.social_reply import social_plan
from elbow_helper.agent.engine.models import AgentDelivery
from tests.agent.discord_actions.test_gif_library import _choice
from tests.agent.engine.test_agent_service import _context


def usage(item_id, posted_at, channel_id=22):
    from elbow_helper.agent.discord_actions.gif_library import clip_family
    return dict(item_id=item_id, clip_family=clip_family(item_id),
                posted_at=posted_at, channel_id=channel_id)


class MemeUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = (Path.cwd() / ('meme-test-' + uuid4().hex)).resolve()
        assert temporary.parent == Path.cwd().resolve()
        temporary.mkdir()
        self.addCleanup(shutil.rmtree, temporary)
        self.path = temporary / 'actions.sqlite3'
        self.repository = AgentActionRepository(self.path)
        self.context = replace(_context(), action_repository=self.repository)

    def record(self, item_id, *, now=None, channel_id=22, guild_id=None):
        row = usage(item_id, time.time() if now is None else now, channel_id)
        self.repository.record_meme(guild_id=guild_id or self.context.guild.id,
            item_id=item_id, clip_family=row['clip_family'], channel_id=channel_id, now=row['posted_at'])

    def test_order_is_fit_then_never_used_then_least_recently_used(self):
        now = 1_000_000
        filler = [usage('other', now - 100_000 + i) for i in range(30)]
        rows = [*filler, usage('a', now - 200_000), usage('b', now - 300_000)]
        for candidates, expected in (
            ([dict(id='a', fit=4), dict(id='b', fit=4), dict(id='c', fit=4)], 'c'),
            ([dict(id='a', fit=4), dict(id='b', fit=4)], 'b'),
            ([dict(id='a', fit=5), dict(id='c', fit=4)], 'a'),
            ([dict(id='c', fit=3)], None),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(select_meme(candidates, usage=rows, now=now), (expected, []))
        self.assertEqual(select_meme([dict(id='c', fit=4)], send_fit=5), (None, []))

    def test_recent_window_is_union_of_last_day_and_last_thirty_with_family_matching(self):
        now = 1_000_000
        for count, age, expected in ((29, 200_000, None), (30, 86_400, None),
                                     (30, 86_401, 'a'), (80, 60, None)):
            rows = [usage('filler', now - i / 100) for i in range(count)] + [usage('a_2', now - age)]
            with self.subTest(count=count, age=age), patch.dict(ITEM_BY_ID, {}, clear=True):
                picked, skipped = select_meme([dict(id='a', fit=5)], usage=rows, now=now)
                self.assertEqual(picked, expected)
                self.assertEqual(skipped, ['a'] if expected is None else [])


    async def test_server_history_changes_real_plan_to_sibling_then_line_and_request_bypasses(self):
        clips = {key: dict(id=key, media=f'https://tenor.com/view/{key}-123')
                 for key in ('a', 'a_2', 'b')}
        self.record('a')
        choice = _choice(meme_shortlist=[dict(id='a', fit=5)])
        with patch.dict(ITEM_BY_ID, clips, clear=True):
            plan = social_plan(choice, 'call', gif_available=True, context=self.context)
            self.assertEqual(plan['steps'][0]['arguments'], {'query': 'a_2'})
            await find_gif(self.context, plan['steps'][0]['arguments'])
            self.assertEqual(self.context.state.gif_item_id, 'a_2')
            self.assertEqual(len(self.repository.recent_memes(guild_id=self.context.guild.id)), 1)
            self.record('a_2')
            self.assertEqual(social_plan(choice, 'call', gif_available=True, context=self.context)['steps'], [])
            self.assertEqual(self.context.state.gif_skipped_recent, ['a'])
            plan = social_plan({**choice, 'gif_requested': True}, 'call', gif_available=True, context=self.context)
            self.assertEqual(plan['steps'][0]['arguments'], {'query': 'a'})
            self.assertEqual(self.context.state.gif_skipped_recent, [])

    def test_pruning_and_server_isolation_persist_after_reopening(self):
        now = RETENTION_SECONDS + 100
        self.record('expired', now=99)
        self.record('boundary', now=100)
        self.record('elsewhere', now=now, guild_id=9999)
        rows = self.repository.recent_memes(guild_id=self.context.guild.id, now=now)
        self.assertEqual([row['item_id'] for row in rows], ['boundary'])
        for index in range(502):
            self.record(str(index), now=now)
        rows = AgentActionRepository(self.path).recent_memes(guild_id=self.context.guild.id, now=now)
        self.assertEqual(len(rows), 500)
        self.assertEqual((rows[0]['item_id'], rows[-1]['item_id']), ('501', '2'))
        self.assertEqual(self.repository.recent_memes(guild_id=9999, now=now)[0]['item_id'], 'elsewhere')
        self.assertEqual(self.repository.recent_memes(guild_id=self.context.guild.id,
                                                    now=now + RETENTION_SECONDS + 1), [])


    async def test_only_confirmed_delivery_records_actual_clip(self):
        message = self.context.source_message
        message.id, message.archive_reply = 300, False
        message.reply = AsyncMock(return_value=SimpleNamespace(id=301))
        item = {'id': 'a_2', 'media': 'https://tenor.com/view/a-123'}
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        with (patch.dict(ITEM_BY_ID, {'a_2': item}, clear=True),
              patch('elbow_helper.agent.conversation.delivery.require_evidence_access', AsyncMock()),
              patch('elbow_helper.agent.conversation.delivery.assert_answer_scope', AsyncMock())):
            await find_gif(self.context, {'query': 'a_2'})
            self.assertEqual(self.repository.recent_memes(guild_id=self.context.guild.id), [])
            for error in (discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no'),
                          AgentDeliveryUnknown('uncertain')):
                message.reply.side_effect = error
                if isinstance(error, AgentDeliveryUnknown):
                    with self.assertRaises(AgentDeliveryUnknown):
                        await surface.send_response(message, '', None, context=self.context)
                else:
                    await surface.send_response(message, '', None, context=self.context)
                self.assertEqual(self.repository.recent_memes(guild_id=self.context.guild.id), [])
                await find_gif(self.context, {'query': 'a_2'})
            message.reply.side_effect = None
            delivery = AgentDelivery()
            await surface.send_response(message, '', None, context=self.context, delivery=delivery)
        rows = self.repository.recent_memes(guild_id=self.context.guild.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['item_id'], rows[0]['clip_family'], rows[0]['channel_id']),
                         ('a_2', 'a', message.channel.id))
        self.assertTrue(delivery.complete)

    async def test_delivery_identity_follows_actual_media_and_ignores_nonlibrary_output(self):
        clips = {key: dict(id=key, media=f'https://tenor.com/view/{key}-123') for key in ('a', 'b')}
        with patch.dict(ITEM_BY_ID, clips, clear=True):
            for url in ('A normal line.', 'https://tenor.com/view/nonlibrary-456', clips['b']['media']):
                await record_delivered_meme(self.repository, guild_id=self.context.guild.id,
                                           channel_id=22, url=url, item_id='a')
        self.assertEqual([row['item_id'] for row in self.repository.recent_memes(guild_id=self.context.guild.id)], ['b'])

    async def test_later_nonce_reconciliation_records_once_without_resending(self):
        url = 'https://tenor.com/view/a-123'
        record = ConversationRecord(request_message_id=300, member_id=42, created_at='now',
            question='Hi', generated_answer='', delivered_answer='', local_context='', evidence=(),
            report_ids=(), reply_ids=(), delivery_complete=False, delivery_unknown=True,
            attempted_nonces=(123,), uncertain_nonce=123, generated_parts=(url,))
        conversation = Conversation(self.context.guild.id, 22)
        conversation.append(ConversationTurn('', frozenset({22}), record=record))
        surface = AgentDeliveryMixin()
        surface.action_repository = self.repository
        surface._find_delivery_nonce = AsyncMock(return_value=SimpleNamespace(id=301))
        surface._conversations = SimpleNamespace(register_reply=Mock())
        surface._archive_reply = AsyncMock()
        with patch.dict(ITEM_BY_ID, {'a': dict(id='a', media=url)}, clear=True):
            self.assertEqual(await surface._reconcile_unknown_deliveries(conversation, SimpleNamespace(id=22)), 1)
            self.assertEqual(await surface._reconcile_unknown_deliveries(conversation, SimpleNamespace(id=22)), 0)
        self.assertEqual([row['item_id'] for row in self.repository.recent_memes(guild_id=self.context.guild.id)], ['a'])
