import json
import unittest

from elbow_helper.agent.engine.account_lists import account_index, render_list
from tests.agent.engine.answer_helpers import insert_account_lists
from tests.agent.engine.answer_helpers import decision_answer
from elbow_helper.configuration.clans import CLANS


ROSTER = {'family': 'read_clan_rosters', 'rows': [
    {'basis': 'current_in_game', 'clan_code': 'BEC', 'player_tag': '#AAA111', 'player_name': 'Synthetic One',
     'townHallLevel': 15},
    {'basis': 'current_in_game', 'clan_code': 'BEM', 'player_tag': '#BBB222', 'player_name': 'Synthetic Two',
     'townHallLevel': 14},
]}
# Historical CWL rows describe where an account attacked, not where it is now.
HISTORY = {'family': 'read_cwl_results', 'rows': [
    {'player_tag': '#BBB222', 'player_name': 'Old Name', 'townhall': 13, 'clan_code': 'BEE'},
    {'player_tag': '#CCC333', 'player_name': 'Departed', 'townhall': 12, 'clan_code': 'BEE'},
]}
COHORT = {'family': 'read_member_cohort', 'basis': 'clan_rosters',
          'clans': [{'clan_code': 'BE1', 'basis': 'current_in_game'}],
          'rows': {'columns': ['member_id', 'accounts'], 'rows': [
              [7, [{'clan_code': 'BE1', 'player_tag': '#DDD444', 'player_name': 'Cohort Account',
                    'townHallLevel': 17}]]]}}
LIMITS = {'unverified_selection': [{'player_tag': '#AAA111', 'player_name': 'Synthetic One', 'clan_code': 'BEC'}]}


def columns():
    return [{'heading': 'Synthetic name', 'fact': 'name'},
            {'heading': 'Synthetic level', 'fact': 'town_hall'},
            {'heading': 'Synthetic estimate', 'value': 'estimate'},
            {'heading': 'Synthetic note', 'value': 'note'},
            {'heading': 'Synthetic flags', 'fact': 'flags'}]


def decision(**overrides):
    value = {'intent': 'Move accounts', 'intent_source': 'request', 'discussion_message_ids': [],
             'settled_choices': [], 'open_choices': [], 'reply': 'Roster:\n[[accounts:roster]]',
             'question': '', 'account_lists': [
                 {'id': 'roster', 'title': 'Synthetic list', 'columns': columns(), 'accounts': [
                     {'account': 'Synthetic One', 'values': {'estimate': '~80%'}},
                     {'account': '#BBB222', 'values': {'estimate': '~40%', 'note': 'no stored wars'}}]}]}
    value.update(overrides)
    return json.dumps(value)


class AccountIndexTests(unittest.TestCase):
    def test_current_clan_comes_only_from_current_rows(self):
        index = account_index({'roster': ROSTER, 'cwl': HISTORY, 'cohort': COHORT}, LIMITS)
        self.assertEqual(index['#BBB222'], {'name': 'Synthetic Two', 'town_hall': 14, 'current_clan': 'BEM'})
        self.assertEqual(index['#CCC333'], {'name': 'Departed', 'town_hall': 12})
        self.assertEqual(index['#DDD444']['current_clan'], 'BE1')
        self.assertTrue(index['#AAA111']['unverified_owner'])


    def test_live_profiles_supply_current_membership_before_or_after_history(self):
        profile = {'family': 'read_member_accounts', 'rows': [
            {'player_tag': '#BBB222', 'player_name': 'Synthetic live name', 'townHallLevel': 16,
             'profile_status': 'ok', 'clan': {'tag': CLANS['BEC'].tag, 'name': 'Synthetic clan'}}]}
        for results in ({'profile': profile, 'history': HISTORY}, {'history': HISTORY, 'profile': profile}):
            with self.subTest(order=list(results)):
                index = account_index(results)
                self.assertEqual(index['#BBB222'], {
                    'name': 'Synthetic live name', 'town_hall': 16, 'current_clan': 'BEC'})
                declaration = {'id': 'a', 'current_clan': 'BEC', 'columns': columns(),
                               'accounts': [{'account': '#BBB222'}]}
                answer, = insert_account_lists(('[[accounts:a]]',), [declaration], index)
                self.assertEqual(answer, 'Synthetic live name — 16')


    def test_unavailable_profiles_and_historical_clans_never_set_current_membership(self):
        for status in (None, 'unavailable', 'stale'):
            with self.subTest(status=status):
                rows = {'family': 'read_cwl_results', 'rows': [
                    {'player_tag': '#HIST', 'player_name': 'Synthetic historical', 'townhall': 12,
                     'clan_code': 'BEC', 'clan': {'tag': CLANS['BEC'].tag, 'name': 'Synthetic clan'},
                     'profile_status': status}]}
                self.assertNotIn('current_clan', account_index({'history': rows})['#HIST'])

    def test_live_outside_clan_is_named_and_clanless_profile_clears_old_roster_location(self):
        for clan, expected in (({'tag': '#OUTSIDE', 'name': '**Synthetic outside**'}, '**Synthetic outside**'),
                               (None, None)):
            with self.subTest(clan=clan):
                profile = {'family': 'read_member_accounts', 'rows': [
                    {'player_tag': '#AAA111', 'player_name': 'Synthetic live',
                     'profile_status': 'ok', 'clan': clan}]}
                index = account_index({'roster': ROSTER, 'profile': profile, 'history': HISTORY})
                self.assertEqual(index['#AAA111'].get('current_clan'), expected)
                if clan:
                    group = {'id': 'a', 'current_clan': 'BEC', 'columns': columns(),
                             'accounts': [{'account': '#AAA111'}]}
                    answer, = insert_account_lists(('[[accounts:a]]',), [group], index)
                    self.assertIn('currently in \\*\\*Synthetic outside\\*\\*', answer)

    def test_clanless_status_preserves_account_index_and_rendered_lists(self):
        account = {'player_tag': '#AAA111', 'player_name': 'Synthetic live',
                   'profile_status': 'ok', 'clan': None}
        declaration = {'id': 'a', 'current_clan': 'BEC', 'columns': columns(),
                       'accounts': [{'account': '#AAA111'}]}
        original = account_index({'roster': ROSTER, 'profiles': {'rows': [account]}})
        expected, = insert_account_lists(('[[accounts:a]]',), [declaration], original)
        self.assertIn('current clan unknown', expected)
        for nested in (False, True):
            with self.subTest(nested=nested):
                row = {**account, 'clan_status': 'not_in_clan'}
                profiles = {'rows': [{'member_id': 42, 'accounts': [row]}] if nested else [row]}
                index = account_index({'roster': ROSTER, 'profiles': profiles})
                self.assertEqual(index, original)
                self.assertNotIn('current_clan', index['#AAA111'])
                rendered, = insert_account_lists(('[[accounts:a]]',), [declaration], index)
                self.assertEqual(rendered, expected)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.index = account_index({'roster': ROSTER, 'cwl': HISTORY}, LIMITS)

    def test_lines_use_source_facts_and_flag_a_wrong_current_clan(self):
        text = render_list({'id': 'move', 'title': 'From BEC', 'current_clan': 'BEC', 'columns': columns(), 'accounts': [
            {'player_tag': '#AAA111', 'values': {'estimate': '~80%'}},
            {'player_tag': '#BBB222', 'values': {'estimate': '~60%'}},
            {'player_tag': '#CCC333'}]}, self.index)
        self.assertEqual(text.splitlines(), [
            '**From BEC (3)**',
            'Synthetic One — 15 — ~80% — owner unverified',
            'Synthetic Two — 14 — ~60% — currently in BEM',
            'Departed — 12 — current clan unknown',
        ])


    def test_shared_names_resolve_by_the_list_clan_or_stay_flagged(self):
        index = {'#EEE555': {'name': 'Shared', 'town_hall': 15, 'current_clan': 'BEM'},
                 '#FFF666': {'name': 'Shared', 'town_hall': 18, 'current_clan': 'BE1'}}
        text, = insert_account_lists(('[[accounts:a]]',), [{'id': 'a', 'current_clan': 'BE1', 'columns': columns(),
                                     'accounts': [{'account': 'Shared', 'values': {'estimate': '~70%'}}]}], index)
        self.assertEqual(text, 'Shared — 18 — ~70%')
        text, = insert_account_lists(('[[accounts:a]]',), [{'id': 'a', 'columns': columns(), 'accounts': [
            {'account': 'Shared', 'values': {'estimate': '~70%'}}]}], index)
        self.assertEqual(text, 'Shared — ~70% — several accounts share this name')


    def test_structural_mistakes_are_returned_for_repair(self):
        for lists, parts, message in (
                ([{'id': 'a', 'columns': columns(), 'accounts': [{'account': '#AAA111'}]}] * 2, ('x',), 'distinct'),
                ([], ('[[accounts:missing]]',), 'declared account list'),
                ([{'id': 'a', 'columns': columns(), 'accounts': [{'account': '#AAA111'}]}], ('[[accounts:a]] [[accounts:a]]',), 'once')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                insert_account_lists(parts, lists, self.index)

    def test_names_cannot_inject_mentions_or_formatting(self):
        index = {'#AAA111': {'name': '@everyone **bold**', 'town_hall': 10}}
        text = render_list({'id': 'a', 'columns': columns(), 'accounts': [{'player_tag': '#AAA111'}]}, index)
        self.assertNotIn('@everyone', text)
        self.assertIn('\\*\\*bold\\*\\*', text)


    def test_heading_cleanup_never_searches_or_removes_an_already_inserted_account_name(self):
        declarations = [{'id': identity, 'title': title,
                         'columns': [{'heading': 'Synthetic label', 'fact': 'name'}],
                         'accounts': [{'account': tag}]}
                        for identity, title, tag in (('a', 'First', '#A'), ('b', 'Second', '#B'))]
        answer, = insert_account_lists(('[[accounts:a]][[accounts:b]]',), declarations,
                                       {'#A': {'name': 'Second'}, '#B': {'name': 'Final account'}})
        self.assertEqual(answer, '**First (1)**\nSecond\n\n**Second (1)**\nFinal account')


class DecisionAnswerTests(unittest.TestCase):
    def setUp(self):
        self.index = account_index({'roster': ROSTER}, LIMITS)


    def test_malformed_list_items_fail_schema_validation(self):
        content = decision(account_lists=[{'id': 'roster', 'accounts': [{'player_tag': '#AAA111'}]}])
        with self.assertRaisesRegex(ValueError, 'Invalid decision answer'):
            decision_answer(content, set(), accounts=self.index)

    def test_markers_inside_the_reply_resolve_from_authorized_data(self):
        reply = '**Selected accounts**\n[[accounts:roster]]\n\n- Review these together.'
        answer = decision_answer(decision(reply=reply), set(), accounts=self.index)
        self.assertEqual(answer, '**Selected accounts**\n\n**Synthetic list (2)**\n'
                         'Synthetic One — 15 — ~80% — owner unverified\n'
                         'Synthetic Two — 14 — ~40% — no stored wars\n\n'
                         '- Review these together.')

    def test_plain_reply_preserves_whitespace_and_has_no_field_length_cap(self):
        for reply in ('\n  - Synthetic finding\n\n    Supporting detail  \n', 'x' * 32001):
            with self.subTest(length=len(reply)):
                self.assertEqual(decision_answer(decision(reply=reply, account_lists=[]), set()), reply)

    def test_private_evidence_envelope_keeps_the_whole_reply(self):
        reply = '**Synthetic result**\n\n- First group\n- Second group\n\nWhich session?'
        content = decision(reply=reply, account_lists=[], intent_source='unresolved',
                           question='Which session?', needs_private_evidence=True)
        self.assertEqual(json.loads(decision_answer(content, set())),
                         {'answer': reply, 'needs_private_evidence': True})

    def test_empty_or_missing_reply_is_rejected(self):
        for reply in ('', '   \n'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                decision_answer(decision(reply=reply, account_lists=[]), set())
        payload = json.loads(decision(account_lists=[]))
        del payload['reply']
        with self.assertRaises(ValueError):
            decision_answer(json.dumps(payload), set())


class FinalAnswerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_composition_indexes_the_rows_it_sends_to_the_writer(self):
        from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
        from tests.agent.engine import test_request_cost as cost
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        # A plain result passes through report restoration unchanged.
        flow.completed_results = {'roster': {'rows': ROSTER['rows']}}
        flow.result_views = dict(flow.completed_results)
        content = decision()
        flow.final_session_factory = lambda **kwargs: cost.Session(
            [AgentStep(content, (), AgentUsage(6000, 800))], [])
        self.assertEqual(await flow._compact_reply(), content)
        self.assertEqual(flow.answer_accounts['#BBB222']['current_clan'], 'BEM')
        answer = decision_answer(content, flow.discussion_message_ids, accounts=flow.answer_accounts)
        self.assertIn('Synthetic Two — 14 — ~40% — no stored wars', answer)


if __name__ == '__main__':
    unittest.main()
