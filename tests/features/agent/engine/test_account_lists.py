import json
import unittest

from elbow_helper.features.agent.engine.account_lists import account_index, insert_account_lists, render_list
from elbow_helper.features.agent.engine.assessment import DECISION_ANSWER_INSTRUCTION, decision_answer
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
             'settled_choices': [], 'open_choices': [], 'facts': 'Roster:\n[[accounts:roster]]',
             'question': '', 'recommendation': '', 'account_lists': [
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

    def test_failed_results_contribute_nothing(self):
        self.assertEqual(account_index({'roster': {**ROSTER, 'error': 'unavailable'}}), {})

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

    def test_nested_live_profiles_and_compact_tables_keep_their_current_clan(self):
        profiles = {'family': 'read_member_cohort', 'rows': {
            'columns': ['member_id', 'accounts'], 'rows': [[9, [
                {'player_tag': '#LIVE', 'player_name': 'Synthetic nested', 'townHallLevel': 13,
                 'profile_status': 'ok', 'clan': {'tag': CLANS['BEM'].tag, 'name': 'Synthetic clan'}}]]]}}
        self.assertEqual(account_index({'profiles': profiles})['#LIVE']['current_clan'], 'BEM')

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

    def test_markers_place_lists_and_unmarked_lists_follow_the_facts(self):
        lists = [{'id': 'a', 'columns': columns(), 'accounts': [{'account': '#AAA111'}]},
                 {'id': 'b', 'columns': columns(), 'accounts': [{'account': 'synthetic two'}]}]
        facts, advice = insert_account_lists(('Facts.', 'Move these:\n[[accounts:a]]'), lists, self.index)
        self.assertEqual(advice, 'Move these:\n\nSynthetic One — 15 — owner unverified')
        self.assertEqual(facts, 'Facts.\n\nSynthetic Two — 14')

    def test_shared_names_resolve_by_the_list_clan_or_stay_flagged(self):
        index = {'#EEE555': {'name': 'Shared', 'town_hall': 15, 'current_clan': 'BEM'},
                 '#FFF666': {'name': 'Shared', 'town_hall': 18, 'current_clan': 'BE1'}}
        text, = insert_account_lists(('[[accounts:a]]',), [{'id': 'a', 'current_clan': 'BE1', 'columns': columns(),
                                     'accounts': [{'account': 'Shared', 'values': {'estimate': '~70%'}}]}], index)
        self.assertEqual(text, 'Shared — 18 — ~70%')
        text, = insert_account_lists(('[[accounts:a]]',), [{'id': 'a', 'columns': columns(), 'accounts': [
            {'account': 'Shared', 'values': {'estimate': '~70%'}}]}], index)
        self.assertEqual(text, 'Shared — ~70% — several accounts share this name')

    def test_list_content_problems_never_sink_the_answer(self):
        text, = insert_account_lists(('[[accounts:a]]',), [{'id': 'a', 'columns': columns(), 'accounts': [
            {'account': 'Nobody Here', 'values': {'estimate': '~10%'}},
            {'account': '#AAA111'}, {'account': 'Synthetic One'}]}], self.index)
        self.assertEqual(text.splitlines(), [
            "Nobody Here — ~10% — couldn't verify this account",
            'Synthetic One — 15 — owner unverified'])

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

    def test_mid_sentence_markers_have_block_boundaries_without_repeated_titles(self):
        group = {'id': 'a', 'title': 'Synthetic / Group', 'columns': columns(),
                 'accounts': [{'account': '#BBB222'}]}
        for prefix in ('Synthetic / Group: ', '**Synthetic / Group (2)**: ',
                       'Lead-in. Synthetic / Group: '):
            with self.subTest(prefix=prefix):
                answer, = insert_account_lists((prefix + '[[accounts:a]] More context.',), [group], self.index)
                self.assertEqual(answer.count('Synthetic / Group'), 1)
                self.assertIn('**Synthetic / Group (1)**\nSynthetic Two — 14\n\nMore context.', answer)
                if prefix.startswith('Lead-in.'):
                    self.assertTrue(answer.startswith('Lead-in.\n\n**'))
        answer, = insert_account_lists(('Before [[accounts:a]] After',), [group], self.index)
        self.assertEqual(answer, 'Before\n\n**Synthetic / Group (1)**\nSynthetic Two — 14\n\nAfter')

    def test_heading_is_bold_and_escapes_model_markdown_and_mentions(self):
        group = {'id': 'a', 'title': '**Synthetic** @everyone (99)', 'columns': columns(),
                 'accounts': [{'account': '#BBB222'}]}
        answer, = insert_account_lists(('[[accounts:a]]',), [group], self.index)
        self.assertTrue(answer.startswith('**\\*\\*Synthetic\\*\\* @\u200beveryone (1)**\n'))

    def test_distinct_lead_in_is_preserved_and_repeated_markers_remain_invalid(self):
        group = {'id': 'a', 'title': 'Synthetic group', 'columns': columns(),
                 'accounts': [{'account': '#BBB222'}]}
        answer, = insert_account_lists(('NotSynthetic group: [[accounts:a]]',), [group], self.index)
        self.assertTrue(answer.startswith('NotSynthetic group:\n\n**Synthetic group'))

    def test_heading_cleanup_never_searches_or_removes_an_already_inserted_account_name(self):
        declarations = [{'id': identity, 'title': title,
                         'columns': [{'heading': 'Synthetic label', 'fact': 'name'}],
                         'accounts': [{'account': tag}]}
                        for identity, title, tag in (('a', 'First', '#A'), ('b', 'Second', '#B'))]
        answer, = insert_account_lists(('[[accounts:a]][[accounts:b]]',), declarations,
                                       {'#A': {'name': 'Second'}, '#B': {'name': 'Final account'}})
        self.assertEqual(answer, '**First (1)**\nSecond\n\n**Second (1)**\nFinal account')

    def test_punctuated_notes_remain_separate_from_flags_with_or_without_a_flag_column(self):
        for punctuation in ('.', '!', '?', ';'):
            for flag_column in (False, True):
                with self.subTest(punctuation=punctuation, flag_column=flag_column):
                    note = 'Synthetic note' + punctuation
                    chosen = [{'heading': 'Synthetic account', 'fact': 'name'},
                              {'heading': 'Synthetic note', 'value': 'note'}]
                    if flag_column:
                        chosen.append({'heading': 'Synthetic flag', 'fact': 'flags'})
                    group = {'id': 'a', 'columns': chosen, 'accounts': [
                        {'account': '#AAA111', 'values': {'note': note}}]}
                    answer, = insert_account_lists(('[[accounts:a]]',), [group], self.index)
                    self.assertEqual(answer, 'Synthetic One — ' + note
                                     + (' — owner unverified' if flag_column else ' (owner unverified)'))


class DecisionAnswerTests(unittest.TestCase):
    def setUp(self):
        self.index = account_index({'roster': ROSTER}, LIMITS)

    def test_resolved_decision_renders_lists_from_rows(self):
        answer = decision_answer(decision(), set(), accounts=self.index)
        self.assertEqual(answer, 'Roster:\n\n**Synthetic list (2)**\nSynthetic One — 15 — ~80% — owner unverified\n'
                                 'Synthetic Two — 14 — ~40% — no stored wars')

    def test_unresolved_decision_keeps_a_requested_list_before_its_question(self):
        content = decision(intent_source='unresolved', open_choices=['Which clans give accounts?'],
                           question='Which clans should give accounts?', facts='Current data only.',
                           recommendation='Assuming an even split:\n[[accounts:roster]]')
        answer = decision_answer(content, set(), accounts=self.index)
        self.assertTrue(answer.startswith('Current data only.\n\nAssuming an even split:\n\n**Synthetic list (2)**'))
        self.assertTrue(answer.endswith('\n\nWhich clans should give accounts?'))

    def test_malformed_list_items_fail_schema_validation(self):
        content = decision(account_lists=[{'id': 'roster', 'accounts': [{'player_tag': '#AAA111'}]}])
        with self.assertRaisesRegex(ValueError, 'Invalid decision answer'):
            decision_answer(content, set(), accounts=self.index)

    def test_instruction_offers_the_list_contract(self):
        self.assertIn('account_lists', DECISION_ANSWER_INSTRUCTION)
        self.assertIn('[[accounts:ID]]', DECISION_ANSWER_INSTRUCTION)


class FinalAnswerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_composition_indexes_the_rows_it_sends_to_the_writer(self):
        from elbow_helper.features.agent.engine.budgets import ASSESSMENT_LIMITS
        from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
        from features.agent.engine import test_request_cost as cost
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
