"""Writer compression retains every typed value, including unknowns."""
import copy
import json
import unittest

from elbow_helper.features.agent.engine.writer_tables import writer_tables


def expand(value):
    if isinstance(value, dict):
        if set(value) in ({'columns', 'rows'}, {'columns', 'rows', 'shared'}):
            records = []
            for cells in value['rows']:
                row = {}
                for path, item in {**value.get('shared', {}), **dict(zip(value['columns'], cells))}.items():
                    target = row
                    parts = path.split('.')
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = expand(item)
                records.append(row)
            return records
        return {key: expand(item) for key, item in value.items()}
    return [expand(item) for item in value] if isinstance(value, list) else value


class WriterTableTests(unittest.TestCase):
    def test_nested_metrics_and_shared_facts_preserve_all_records_and_unknowns(self):
        rows = [{'player_tag': str(index), 'leadership': {'status': 'unknown', 'roles': []},
            'measures': {'used': index, 'expected': index + 2,
                         'rate': {'numerator': index, 'denominator': index + 2,
                                  'value': index / (index + 2)}},
            'optional': None if index % 2 else {'value': index},
            'empty': {}, 'events': [{'id': str(index), 'value': index}]}
            for index in range(120)]
        original = copy.deepcopy(rows)
        result = writer_tables({'accounts': rows})
        self.assertEqual(expand(result), {'accounts': original})
        self.assertEqual(rows, original)
        self.assertLess(len(json.dumps(result)), len(json.dumps(rows)) * .6)
        self.assertIn('measures.rate.value', result['accounts']['columns'])
        self.assertEqual(result['accounts']['shared']['leadership.status'], 'unknown')

    def test_existing_column_tables_are_reprojected_without_losing_rows(self):
        rows = [{'id': str(i), 'metric': {'value': i, 'complete': True}} for i in range(30)]
        result = writer_tables({'columns': ['id', 'metric'],
                                'rows': [[row['id'], row['metric']] for row in rows]})
        self.assertEqual(expand(result), rows)
        self.assertEqual(writer_tables(result), result)

    def test_null_false_zero_and_float_are_not_shared_as_equal_values(self):
        rows = [{'id': str(i), 'value': [None, False, 0, 0.0, True, 1][i % 6]}
                for i in range(12)]
        result = writer_tables(rows)
        self.assertEqual(json.dumps(expand(result), sort_keys=True), json.dumps(rows, sort_keys=True))

    def test_empty_table_keeps_its_schema(self):
        value = {'columns': ['id', 'metric'], 'rows': []}
        self.assertEqual(writer_tables(value), value)

    def test_literal_dotted_keys_are_not_flattened_into_a_collision(self):
        rows = [{'a.b': i, 'a': {'b': i + 1}} for i in range(12)]
        result = writer_tables(rows)
        self.assertEqual(result, rows)
