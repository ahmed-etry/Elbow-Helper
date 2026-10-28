"""Bounded evidence retains later replies across independently read discussions."""
import json
import unittest

from elbow_helper.agent.engine.tool_call import bound_tool_result


class DiscussionBudgetTests(unittest.TestCase):
    def test_complete_measure_tables_use_token_room_without_an_unrelated_character_cutoff(self):
        from elbow_helper.agent.engine.answer_inputs import bounded_answer_evidence
        from elbow_helper.agent.conversation.context import estimate_tokens
        from agent.engine.test_writer_tables import expand
        rows = [{'player_tag': str(i), 'details': 'x' * 1600} for i in range(200)]
        evidence = {'accounts': {'rows': rows}}
        encoded = json.dumps({'results': evidence}, ensure_ascii=False, separators=(',', ':'))
        actual = bounded_answer_evidence(evidence, estimate_tokens(encoded) + 1000, None)
        self.assertEqual(expand(json.loads(actual))['results']['accounts']['rows'], rows)
        self.assertNotIn('truncated', json.loads(actual))

    def test_large_discussions_share_pressure_and_keep_later_replies(self):
        results = {name: {'rows': [{'message_id': start + i, 'content': 'x' * 180}
                                 for i in range(40)]}
                   for name, start in (('first', 100), ('second', 200))}
        encoded = bound_tool_result(json.dumps({'results': results}), 5500)
        self.assertLessEqual(len(encoded), 5500)
        decoded = json.loads(encoded)
        for name, latest in (('first', 139), ('second', 239)):
            rows = decoded['results'][name]['rows']
            self.assertTrue(rows)
            self.assertEqual(rows[-1]['message_id'], latest)
            self.assertEqual(rows, sorted(rows, key=lambda row: row['message_id']))
            self.assertEqual(decoded['truncated_collections'][f'results.{name}.rows']['total_records'], 40)

    def test_compact_message_tables_keep_newest_whole_records(self):
        rows = [[i, i - 1, 'x' * 180] for i in range(100, 140)]
        payload = {'rows': {'columns': ['message_id', 'reply_to_message_id', 'content'], 'rows': rows}}
        result = json.loads(bound_tool_result(json.dumps(payload), 1200))
        selected = result['rows']['rows']
        self.assertTrue(selected)
        self.assertEqual(selected[-1][0], 139)
        self.assertTrue(all(row in rows for row in selected))

    def test_duplicate_message_ids_do_not_exceed_the_record_count(self):
        payload = {'rows': [{'message_id': 100, 'content': 'x' * 400} for _ in range(20)]}
        encoded = bound_tool_result(json.dumps(payload), 1200)
        result = json.loads(encoded)
        self.assertLessEqual(len(encoded), 1200)
        self.assertEqual(len(result['rows']), result['truncated_collections']['rows']['returned_records'])
