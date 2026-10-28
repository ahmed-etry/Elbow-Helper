"""Per-account attack results and unobserved cohort members remain distinct."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.families import war_reads
from agent.engine.test_agent_plan_flow import _context


class WarAccountMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_account_measurements_and_unknown_participation(self):
        owners = {'#P0': 81, '#Q0': 82, '#Y0': 83}
        links = SimpleNamespace(get_links_by_tags=lambda tags: {tag: {'discord_user_id': owners[tag]} for tag in tags})
        context = replace(_context(), account_links=links)
        rows = [{'war_id': '#R0', 'player_tag': tag, 'player_name': tag,
            'attacks_used': 1, 'attacks_expected': 2, 'missed': 1,
            'observed_attack_performance': {'attacks': 1, 'stars': stars, 'stars_measured': 1,
                'triples': int(stars == 3), 'destruction_total': stars * 30, 'destruction_measured': 1}}
            for tag, stars in (('#P0', 3), ('#Q0', 1))]
        with patch.object(war_reads, 'selected_wars', AsyncMock(return_value={'rows': rows, 'war_ids': ['#R0']})):
            result = await war_reads.attendance(context, {'war_type': 'REG', 'account_tags': list(owners)})
            by_tag = {row['player_tag']: row for row in result['rows']}
            self.assertEqual(by_tag['#P0']['attack_performance']['stars'], 3)
            self.assertEqual(by_tag['#Q0']['attack_performance']['stars'], 1)
            self.assertIsNone(by_tag['#Y0']['attacks_used'])
            self.assertEqual(by_tag['#Y0']['attack_performance']['average_stars']['denominator'], 0)
            member_result = await war_reads.attendance(context, {'war_type': 'REG', 'account_tags': list(owners), 'unit': 'member'})
            self.assertEqual({row['member_id'] for row in member_result['rows']}, {81, 82, 83})
