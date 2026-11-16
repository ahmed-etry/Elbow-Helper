import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.agent.reads.ranked import checked_request
from tests.agent.reads.test_compact_clash import read_context, synthetic_clan
from tests.agent.reads.test_ranked import AFTER, BEFORE


class PrefixedClanCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_clan_code_with_hash_resolves_to_the_same_clan_tag(self):
        clan = synthetic_clan()
        metadata = SimpleNamespace(tag=clan['tag'], cwl_only=False)
        with patch('elbow_helper.agent.reads.clash.CLANS', {'BIRCH': metadata}), \
             patch('elbow_helper.agent.reads.ranked.CLANS', {'BIRCH': metadata}), \
             patch('elbow_helper.agent.reads.clash.require_evidence_access', AsyncMock()):
            for spelling in ('BIRCH', '#BIRCH', '#birch', clan['tag']):
                with self.subTest(spelling=spelling):
                    context = read_context({clan['tag']: clan})
                    result = await read_clash(context, {'kind': 'clan', 'tags': [spelling]})
                    self.assertNotIn('error', result)
                    self.assertEqual(result['items'][0]['tag'], clan['tag'])
                    window = checked_request({'clan_tags': [spelling], 'after': AFTER, 'before': BEFORE}, 200)
                    self.assertNotIsInstance(window, dict)
                    self.assertEqual(window.values, (clan['tag'],))
