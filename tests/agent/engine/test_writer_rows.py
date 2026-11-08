"""Writer rows shrink per row, but every row, marker and listing stays."""
import json
import unittest

from elbow_helper.agent.engine.answer_inputs import answer_evidence, serialized_answer_evidence
from elbow_helper.agent.engine.writer_rows import compact_writer_rows


def ratio(numerator, denominator):
    if not denominator:
        return {'numerator': numerator, 'denominator': denominator, 'value': None, 'display': 'unavailable'}
    value = numerator / denominator
    return {'numerator': numerator, 'denominator': denominator, 'value': value, 'display': f'{value:.2f}'}


def war_row(index, *, observed=True):
    attacks = 6 if observed else 0
    row = {'member_id': 1000 + index, 'player_tag': f'#T{index}', 'player_name': f'Player {index}',
           'account_tags': [f'#T{index}'], 'attacks_used': attacks, 'attacks_expected': 6, 'missed': 6 - attacks,
           'unit': 'account', 'missing_link': False, 'totals_complete': observed,
           'participation_status': 'warring' if observed else 'unknown', 'wars_observed': 3,
           'attack_performance': {'wars': 3, 'observed_attacks': attacks, 'stars_measured': attacks,
               'stars': attacks * 2, 'average_stars': ratio(attacks * 2, attacks),
               'triple_rate_percent': ratio(attacks, attacks * 2),
               'measurement_status': 'observed' if observed else 'no_attack_measurements'}}
    if not observed:
        row['observation_status'] = 'no_stored_participation'
    return row


def war_result(count=24):
    rows = [war_row(index, observed=index % 4 != 0) for index in range(count)]
    return {'family': 'read_war_attendance', 'rows': rows, 'total_rows': count, 'count': count,
            'opportunity_unit': 'account-war', 'coverage': {'query_complete': True}}


class WriterRowTests(unittest.TestCase):
    def test_ratios_keep_their_display_including_unavailable(self):
        facts = compact_writer_rows(war_result(), 'read_war_attendance')
        text = json.dumps(facts)
        self.assertNotIn('numerator', text)
        self.assertNotIn('denominator', text)
        self.assertIn('unavailable', text)
        self.assertIn('2.00', text)

    def test_every_row_and_every_missing_marker_survives(self):
        source = war_result()
        evidence = answer_evidence({'wars': source})['wars']
        rendered = json.loads(serialized_answer_evidence({'wars': evidence}))['results']['wars']
        self.assertEqual(rendered['total_rows'], 24)
        table = rendered['rows']
        self.assertIsInstance(table, dict)
        columns = table['columns']
        rows = [dict(zip(columns, row)) for row in table['rows']]
        self.assertEqual(len(rows), 24)
        self.assertEqual([row['player_tag'] for row in rows], [f'#T{index}' for index in range(24)])
        unknown = [row for row in rows if row['participation_status'] == 'unknown']
        self.assertEqual(len(unknown), 6)
        self.assertTrue(all(row['totals_complete'] is False for row in unknown))
        self.assertTrue(all(row['observation_status'] == 'no_stored_participation' for row in unknown))
        self.assertTrue(all(row['attack_performance.measurement_status'] == 'no_attack_measurements'
                            for row in unknown))
        self.assertTrue(all(row['attack_performance.average_stars'] == 'unavailable' for row in unknown))
        # Optional fields are null where the source had none, and the result says so.
        self.assertEqual(evidence['row_rendering']['rows']['null_when_absent'], ['observation_status'])
        self.assertTrue(all(row['observation_status'] is None for row in rows
                            if row['participation_status'] == 'warring'))


    def test_a_field_that_differs_on_one_row_stays_on_every_row(self):
        source = war_result()
        source['rows'][5]['account_tags'] = ['#T5', '#ALT']
        source['rows'][7]['missing_link'] = True
        facts = answer_evidence({'wars': source})['wars']
        self.assertTrue(all('account_tags' in row for row in facts['rows']))
        self.assertTrue(facts['rows'][5]['account_tags'] == ['#T5', '#ALT'])
        self.assertNotIn('missing_link', facts['row_rendering']['rows']['same_on_every_row'])
        self.assertEqual([row['missing_link'] for row in facts['rows']].count(True), 1)


    def test_a_ratio_without_a_display_is_kept_whole(self):
        odd = {'numerator': 1, 'denominator': 2, 'value': 0.5, 'display': None}
        facts = compact_writer_rows({'rows': [{'member_id': 1, 'metric': odd}]}, 'read_member_accounts')
        self.assertEqual(facts['rows'][0]['metric'], odd)


    def test_reports_keep_their_original_rows(self):
        source = war_result()
        original = json.dumps(source, sort_keys=True)
        answer_evidence({'wars': source})
        self.assertEqual(json.dumps(source, sort_keys=True), original)


if __name__ == '__main__':
    unittest.main()
