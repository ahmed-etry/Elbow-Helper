"""Library coverage, stable prompts and exact joke selection."""
import unittest
from unittest.mock import patch

from elbow_helper.agent.discord_actions.gif_library import LIBRARY, ITEM_BY_ID, library_gif
from elbow_helper.agent.discord_actions.gifs import find_gif
from elbow_helper.agent.engine.social_reply import MEME_SEND_FIT, salvage_choice, social_plan
from tests.agent.engine.test_agent_service import _context

ORIGINAL_MEDIA = {'reaction-24.jpg', 'smart-ass.gif', 'reaction-04.jpg', 'reaction-05.jpg', 'reaction-21.webp', 'reaction-15.gif', 'reaction-20.webp', 'reaction-27.jpg', 'bait-or-low-iq.gif', 'is-this-guy-really-talking-back-to-me-rn.gif', 'reaction-13.gif', 'reaction-22.jpg', 'reaction-09.png', 'son-ts-was-not-tuff.gif', 'reaction-07.jpg', 'ts-lowkey-tuff.gif', 'reaction-12.jpg', 'reaction-18.jpg', 'reaction-06.gif', 'my-fault-g.gif', 'reaction-14.gif', 'reaction-02.jpg', 'reaction-08.gif', 'reaction-28.gif', 'reaction-10.gif', 'reaction-11.png', 'reaction-26.jpg', 'reaction-17.jpg', 'reaction-01.jpg', 'reaction-25.jpg', 'watch-yo-tone-mf.gif', 'reaction-16.gif', 'reaction-19.gif', 'reaction-23.jpg'}


def _choice(**overrides):
    return {'mechanic_check': 'no_gameplay_number', 'reaction_fit': False, 'reaction_basis': '',
            'reply_form': 'text', 'text': 'Still works.', 'gif_requested': False,
            'meme_shortlist': [{'id': 'ts_lowkey_tuff', 'fit': MEME_SEND_FIT},
                               {'id': 'things_will_happen_horse', 'fit': 2},
                               {'id': 'son_not_tuff', 'fit': 1}], 'reactions': [], **overrides}


class GifLibraryTests(unittest.IsolatedAsyncioTestCase):


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
