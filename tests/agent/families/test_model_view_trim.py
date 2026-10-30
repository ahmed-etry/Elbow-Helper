"""The model sees war facts, not ID lists or source bookkeeping."""
import json
import unittest

from elbow_helper.agent.families.model_view import family_model_view


def _payload(family, accounts, wars=60):
    war_ids = [f'W{i:04d}-synthetic' for i in range(wars)]
    rows = [{'player_tag': f'#P{i}', 'player_name': f'Player {i}', 'war_ids': war_ids[:10],
             'attacks_used': 8, 'attacks_expected': 10, 'missed': 2} for i in range(accounts)]
    return {'family': family, 'rows': rows, 'war_ids': war_ids, 'clan_participation': [],
            'provenance': {'source_channels': list(range(1, 30)), 'access': ['synthetic']}}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


class ModelViewTrimTests(unittest.TestCase):
    def test_war_reads_drop_id_lists_and_provenance_but_keep_counts(self):
        for family in ('read_war_attendance', 'read_cwl_results'):
            for accounts in (1, 20):
                with self.subTest(family=family, accounts=accounts):
                    payload = _payload(family, accounts)
                    view = family_model_view(payload)
                    self.assertNotIn('war_ids', view)
                    self.assertNotIn('provenance', view)
                    self.assertEqual(view['wars_checked'], 60)
                    self.assertTrue(all(row['wars_observed'] == 10 for row in view['rows']))
                    self.assertLess(_size(view), _size(payload))
                    # The retained payload is not mutated.
                    self.assertEqual(len(payload['war_ids']), 60)
                    self.assertIn('provenance', payload)

    def test_second_projection_keeps_the_checked_count(self):
        view = family_model_view(_payload('read_war_attendance', 2))
        again = family_model_view(view)
        self.assertEqual(again['wars_checked'], 60)

    def test_missing_war_ids_still_report_zero_checked(self):
        payload = _payload('read_war_attendance', 1)
        del payload['war_ids']
        self.assertEqual(family_model_view(payload)['wars_checked'], 0)

    def test_provenance_is_dropped_for_other_families(self):
        view = family_model_view({'family': 'read_member_accounts', 'rows': [], 'provenance': {'x': 1}})
        self.assertNotIn('provenance', view)

    def test_message_rows_drop_source_but_cited_messages_still_link(self):
        from elbow_helper.agent.engine.grounding import ground_answer
        from elbow_helper.agent.plan.results import model_view
        link = 'https://discord.com/channels/3/91/{}'
        rows = [{'message_id': 100 + i, 'channel_id': 91, 'channel': 'synthetic', 'author_id': 200 + i,
                 'author': f'Member {i}', 'timestamp': '2026-01-01T00:00:00+00:00',
                 'content': f'Synthetic text {i}', 'content_complete': True, 'source': link.format(100 + i)}
                for i in range(12)]
        for mode in ('channel_window', 'search'):
            with self.subTest(mode=mode):
                payload = {'kind': 'family', 'family': 'read_conversation_evidence', 'mode': mode,
                           'channel_id': 91, 'rows': [dict(row) for row in rows]}
                view = family_model_view(payload)
                self.assertTrue(all('source' not in row for row in view['rows']))
                self.assertLess(_size(view), _size(payload))
                self.assertTrue(all('source' in row for row in payload['rows']))
                shown = model_view(payload)
                self.assertNotIn('discord.com', json.dumps(shown))
                self.assertEqual(ground_answer('See [[103]]', {'read': shown}, 3),
                                 f"See [Member 3]({link.format(103)})")
                quoted = ground_answer('[[message:105]]', {'read': shown}, 3)
                self.assertIn(link.format(105), quoted)
                self.assertIn('Synthetic text 5', quoted)
                # A model-typed URL for the wrong channel is bound to the read message.
                self.assertIn(link.format(104), ground_answer(
                    'https://discord.com/channels/3/7/104', {'read': shown}, 3))

    def test_roster_ownership_keeps_counts_not_the_owner_list(self):
        owners = [{'member_id': i, 'member_name': f'Member {i}', 'account_count': 2} for i in range(30)]
        payload = {'family': 'read_clan_rosters', 'rows': [], 'clans': [{
            'clan_code': 'ZZ', 'ownership': {'linked_accounts': 60, 'unknown_owner_accounts': 1,
                                             'distinct_linked_owners': 30, 'owners': owners,
                                             'basis': 'synthetic'}}]}
        view = family_model_view(payload)
        self.assertEqual(view['clans'][0]['ownership'], {
            'linked_accounts': 60, 'unknown_owner_accounts': 1,
            'distinct_linked_owners': 30, 'basis': 'synthetic'})
        self.assertLess(_size(view), _size(payload))
        self.assertEqual(len(payload['clans'][0]['ownership']['owners']), 30)

    def test_other_families_keep_source_and_ownership_untouched(self):
        payload = {'family': 'read_member_accounts', 'rows': [{'source': 'x', 'ownership': {'owners': [1]}}]}
        self.assertEqual(family_model_view(payload)['rows'], payload['rows'])


if __name__ == '__main__':
    unittest.main()
