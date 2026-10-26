"""Library coverage, stable prompts and exact joke selection."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.features.agent.discord_actions.gif_library import (
    GROUPS, LIBRARY, ITEM_BY_ID, catalogue_text, channel_gif_urls, library_gif,
)
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.engine.social_reply import MEME_SEND_FIT, social_reply_definition, salvage_choice, social_plan
from elbow_helper.features.agent.plan.checker import valid_arguments
from features.agent.engine.test_agent_service import _context

ORIGINAL_MEDIA = {'reaction-24.jpg', 'smart-ass.gif', 'reaction-04.jpg', 'reaction-05.jpg', 'reaction-21.webp', 'reaction-15.gif', 'reaction-20.webp', 'reaction-27.jpg', 'bait-or-low-iq.gif', 'is-this-guy-really-talking-back-to-me-rn.gif', 'reaction-13.gif', 'reaction-22.jpg', 'reaction-09.png', 'son-ts-was-not-tuff.gif', 'reaction-07.jpg', 'ts-lowkey-tuff.gif', 'reaction-12.jpg', 'reaction-18.jpg', 'reaction-06.gif', 'my-fault-g.gif', 'reaction-14.gif', 'reaction-02.jpg', 'reaction-08.gif', 'reaction-28.gif', 'reaction-10.gif', 'reaction-11.png', 'reaction-26.jpg', 'reaction-17.jpg', 'reaction-01.jpg', 'reaction-25.jpg', 'watch-yo-tone-mf.gif', 'reaction-16.gif', 'reaction-19.gif', 'reaction-23.jpg'}


def _choice(**overrides):
    return {'mechanic_check': 'no_gameplay_number', 'reaction_fit': False, 'reaction_basis': '',
            'reply_form': 'text', 'text': 'Still works.', 'gif_requested': False,
            'meme_shortlist': [{'id': 'ts_lowkey_tuff', 'fit': MEME_SEND_FIT},
                               {'id': 'things_will_happen_horse', 'fit': 2},
                               {'id': 'son_not_tuff', 'fit': 1}], 'reactions': [], **overrides}


class GifLibraryTests(unittest.IsolatedAsyncioTestCase):
    def test_all_34_original_media_remain_with_real_use_and_screen_descriptions(self):
        local_items = [item for item in LIBRARY if not item['media'].startswith('https://')]
        self.assertEqual({item['media'] for item in local_items}, ORIGINAL_MEDIA)
        self.assertEqual(len(local_items), len(ORIGINAL_MEDIA))
        self.assertEqual(len(ITEM_BY_ID), len(LIBRARY))
        for item in LIBRARY:
            self.assertEqual(set(item), {'id', 'media', 'use', 'shows', 'group'})
            self.assertRegex(item['id'], r'^[a-z][a-z0-9_]*$')
            self.assertIn(item['group'], GROUPS)
            self.assertGreater(len(item['use']), 20)
            self.assertGreater(len(item['shows']), 20)
        self.assertIn('sarcastically', ITEM_BY_ID['ts_lowkey_tuff']['use'])
        self.assertIn('stupid', ITEM_BY_ID['why_i_bomb_people']['use'])

    async def test_community_links_deliver_the_exact_chosen_clip_without_search(self):
        community_items = [item for item in LIBRARY if item['media'].startswith('https://')]
        self.assertTrue(community_items)
        for item in community_items:
            with self.subTest(meme_id=item['id']):
                context = _context()
                self.assertEqual(await find_gif(context, {'query': item['id']}),
                                 {'url': item['media']})
                self.assertEqual(context.state.gif_url, item['media'])
                self.assertIsNone(context.state.gif_attachment)

    def test_enum_and_catalogue_are_identical_across_requests(self):
        first = social_reply_definition({}).parameters
        second = social_reply_definition({'find_gif': object()}, combo_due=True).parameters
        shortlist = first['properties']['meme_shortlist']
        self.assertEqual(shortlist, second['properties']['meme_shortlist'])
        item = shortlist['items']
        self.assertEqual(item['properties']['id']['enum'], list(ITEM_BY_ID))
        self.assertEqual(item['properties']['fit'], {'type': 'integer', 'minimum': 1, 'maximum': 5})
        self.assertEqual(item['required'], ['id', 'fit'])
        self.assertFalse(item['additionalProperties'])
        self.assertEqual((shortlist['minItems'], shortlist['maxItems'], shortlist['uniqueItems']), (3, 3, True))
        self.assertTrue(shortlist['description'].startswith(
            'The three library memes closest to this moment. Rate how well posting each one '
            'alone answers this message: 5 = a member here would post it as the reply, '
            '4 = it would work but a line is just as natural, 3 = related but off, 1-2 = wrong. '))
        for order in (list(first['properties']), first['required']):
            start = order.index('meme_shortlist')
            self.assertEqual(order[start:start + 2], ['meme_shortlist', 'text'])
            self.assertNotIn('meme', order)
            self.assertNotIn('meme_match', order)
        catalogue = catalogue_text()
        self.assertEqual(shortlist['description'].split('Items by group, as id = when members post it [what it shows]:\n', 1)[1].encode(),
                         catalogue.encode())
        for item in LIBRARY:
            self.assertEqual(catalogue.count(f"{item['id']} = {item['use']} [{item['shows']}]"), 1)
        self.assertTrue(valid_arguments(_choice(), first))
        for invalid in ([], _choice()['meme_shortlist'][:2], _choice()['meme_shortlist'] * 2,
                        [_choice()['meme_shortlist'][0]] * 3):
            self.assertFalse(valid_arguments(_choice(meme_shortlist=invalid), first))

    def test_top_fit_and_first_listed_tie_control_delivery(self):
        ids = [item['id'] for item in _choice()['meme_shortlist']]
        below = MEME_SEND_FIT - 1
        for fits, expected in (((1, MEME_SEND_FIT, 2), ids[1]), ((below, MEME_SEND_FIT, 2), ids[1]),
                               ((1, 2, MEME_SEND_FIT), ids[2]), ((MEME_SEND_FIT,) * 3, ids[0]),
                               ((below,) * 3, None),
                               ((below, 2, 1), None)):
            with self.subTest(fits=fits):
                shortlist = [dict(id=item, fit=fit) for item, fit in zip(ids, fits)]
                choice = salvage_choice(_choice(meme_shortlist=shortlist), {})
                plan = social_plan(choice, 'synthetic', gif_available=True)
                self.assertEqual(plan['reply'], 'Still works.')
                self.assertEqual([step['arguments']['query'] for step in plan['steps']],
                                 [expected] if expected else [])

    def test_requested_gif_uses_the_highest_fit_at_every_valid_score(self):
        ids = [item['id'] for item in _choice()['meme_shortlist']]
        for fit in range(1, 6):
            shortlist = [dict(id=ids[0], fit=1), dict(id=ids[1], fit=fit), dict(id=ids[2], fit=1)]
            choice = salvage_choice(_choice(meme_shortlist=shortlist, gif_requested=True), {})
            plan = social_plan(choice, 'synthetic', gif_available=True)
            self.assertEqual(plan['steps'][0]['arguments']['query'], ids[0] if fit == 1 else ids[1])
            self.assertEqual(social_plan(choice, 'synthetic', gif_available=False)['steps'], [])

    def test_bad_entries_are_dropped_individually_without_mutating_the_choice(self):
        from copy import deepcopy
        first, second, third = _choice()['meme_shortlist']
        bad_entries = [{'id': 'bogus', 'fit': 5}, {'id': 'none', 'fit': 5},
                       *({'id': third['id'], 'fit': fit} for fit in (None, True, False, '5', 4.0, 0, 6)),
                       {'id': third['id']}, {'fit': 5}, {'id': third['id'], 'fit': 5, 'extra': True},
                       None, 42, third['id'], [], {'id': [], 'fit': 5},
                       {**first, 'fit': 5}]
        registry = {'react_to_request': object()}
        for bad in bad_entries:
            for requested in (False, True):
                with self.subTest(bad=bad, requested=requested):
                    retained = {**first, 'fit': 2 if requested else MEME_SEND_FIT}
                    raw = _choice(meme_shortlist=[retained, bad, second], gif_requested=requested,
                                  reply_form='text_reactions', reaction_fit=True,
                                  reaction_basis='The message celebrates a win.', reactions=['\U0001f389'])
                    before = deepcopy(raw)
                    choice = salvage_choice(raw, registry)
                    self.assertEqual(choice['meme_shortlist'], [retained, second])
                    self.assertEqual(choice['gif_requested'], requested)
                    self.assertEqual(choice['reactions'], raw['reactions'])
                    plan = social_plan(choice, 'synthetic', gif_available=True)
                    self.assertEqual([(step['capability'], step['arguments']) for step in plan['steps']],
                                     [('find_gif', {'query': first['id']}),
                                      ('react_to_request', {'emoji': raw['reactions']})])
                    self.assertEqual(raw, before)

    def test_no_valid_shortlist_keeps_text_and_reactions_even_when_requested(self):
        registry = {'react_to_request': object()}
        for invalid in ([], [None], None, 'ts_lowkey_tuff', {},
                        [{'id': 'unknown', 'fit': 5}]):
            with self.subTest(shortlist=invalid):
                raw = _choice(meme_shortlist=invalid, gif_requested=True, reply_form='text_reactions',
                              reaction_fit=True, reaction_basis='The message celebrates a win.',
                              reactions=['\U0001f389'])
                choice = salvage_choice(raw, registry)
                self.assertEqual(choice['meme_shortlist'], [])
                self.assertEqual(choice['reactions'], raw['reactions'])
                plan = social_plan(choice, 'synthetic', gif_available=True)
                self.assertEqual(plan['reply'], 'Still works.')
                self.assertEqual([step['capability'] for step in plan['steps']], ['react_to_request'])
        raw.pop('meme_shortlist')
        self.assertEqual(salvage_choice(raw, registry)['meme_shortlist'], [])

    def test_malformed_reactions_do_not_disable_a_valid_meme(self):
        choice = salvage_choice(_choice(reply_form='invalid', reactions=[None],
                                        reaction_fit='true', reaction_basis=42), {'react_to_request': object()})
        self.assertEqual(choice['reactions'], [])
        self.assertEqual(social_plan(choice, 'synthetic', gif_available=True)['steps'][0]['capability'], 'find_gif')

    def test_channel_snapshot_keeps_full_urls_and_attachments(self):
        rows = [SimpleNamespace(content='https://tenor.com/view/reaction-Abc123', embeds=[]),
                SimpleNamespace(content='', embeds=[SimpleNamespace(url='https://media.tenor.com/Def456.gif')]),
                SimpleNamespace(content='', attachments=[SimpleNamespace(filename='reaction.jpg')])]
        self.assertEqual(channel_gif_urls(rows), {'https://tenor.com/view/reaction-Abc123', 'https://media.tenor.com/Def456.gif', 'attachment://reaction.jpg'})
        self.assertEqual(channel_gif_urls([SimpleNamespace(content='https://[', embeds=[])]), set())

    async def test_repeated_item_uses_unused_clip_of_same_joke_then_returns_nothing(self):
        clips = {key: {'id': key, 'media': media} for key, media in (
            ('joke', 'https://tenor.com/view/Seen123'), ('joke_2', 'https://tenor.com/view/fresh-456'),
            ('different', 'https://tenor.com/view/Other789'))}
        context = _context()
        with patch.dict(ITEM_BY_ID, clips, clear=True):
            self.assertEqual(library_gif(context, 'joke'), clips['joke']['media'])
            context.state.channel_gif_urls.add(clips['joke']['media'])
            self.assertEqual(library_gif(context, 'joke'), clips['joke_2']['media'])
            context.state.used_gif_urls.add(clips['joke_2']['media'])
            self.assertIsNone(library_gif(context, 'joke'))
            context.state.used_gif_urls.add(clips['joke']['media'])
            self.assertIsNone(library_gif(context, 'joke'))
            self.assertEqual(await find_gif(context, {'query': 'different'}),
                             {'url': clips['different']['media']})

    async def test_unknown_item_leaves_gif_unselected(self):
        context = _context()
        self.assertEqual(await find_gif(context, {'query': 'not_a_meme'}),
                         {'error': 'No fitting GIF in the library.'})
        self.assertIsNone(context.state.gif_url)
        self.assertIsNone(context.state.gif_attachment)
