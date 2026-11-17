"""A Clash read cut to fit keeps the fields the plan selected, with true row counts."""

import json
import unittest

from elbow_helper.agent.engine.tool_call import bound_tool_result


def battle_log(fields):
    items = []
    for account in range(3):
        rows = [{'armyShareCode': f'u{account}x{battle}-' + 'a' * 60, 'battleTimestamp': f'2026-10-{battle:02d}',
                 'attack': True} for battle in range(50)]
        items.append({'tag': f'#SYN{account}', 'status': 'ok', 'data': {'items': rows},
                      'row_counts': {'/items': {'returned_rows': 50, 'total_rows': 50}}, 'truncated': False})
    return {'source': 'clash_api', 'kind': 'battle_log', 'items': items, 'projection': 'fields',
            'fields': fields, 'truncated': False, 'flags': {'status': 'complete', 'truncated': False}}


class ClashFieldBoundTests(unittest.TestCase):
    def test_selected_fields_survive_a_cut_for_every_account(self):
        # A leadership question about armies asked three accounts' battle logs for their army codes.
        payload = battle_log(['items.armyShareCode', 'items.battleTimestamp', 'items.attack'])
        content = json.dumps(payload)
        bounded = json.loads(bound_tool_result(content, len(content) // 2))
        self.assertEqual(bounded['projection'], 'fields')
        for item in bounded['items']:
            rows = item['data']['items']
            rows = [dict(zip(rows['columns'], row)) for row in rows['rows']] if isinstance(rows, dict) else rows
            self.assertTrue(rows)
            self.assertTrue(all(row['armyShareCode'] for row in rows))
            self.assertEqual(item['row_counts']['/items'], {'returned_rows': len(rows), 'total_rows': 50})
        self.assertEqual(len(bounded['items']), 3)
