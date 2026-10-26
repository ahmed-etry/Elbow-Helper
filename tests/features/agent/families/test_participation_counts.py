"""War participation separates attacks, confirmed non-attendance and unknowns."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.families import war_reads
from elbow_helper.features.agent.engine.answer_inputs import answer_evidence
from features.agent.engine.test_agent_plan_flow import _context


class ParticipationCountsTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_clan_members_deduplicate_alts_and_preserve_unlinked_and_unknown(self):
        def row(tag, clan, used, missed=0):
            return {'player_tag': tag, 'player_name': tag, 'clan_code': clan,
                    'war_id': clan + '-war', 'attacks_used': used,
                    'attacks_expected': 2 if missed is not None else None, 'missed': missed}
        rows = [row('#P0', 'BE1', 2), row('#Q0', 'BE1', 0, 2),
                row('#P2', 'BE1', 0, 2), row('#Q2', 'BE1', 0, None),
                row('#P8', 'BE1', 1), row('#P0', 'BEE', 1)]
        context = _context()
        context = replace(context, account_links=SimpleNamespace(
            get_links_by_tags=lambda _: {tag: {'discord_user_id': owner}
                for tag, owner in (('#P0', 1), ('#Q0', 1), ('#P2', 2), ('#Q2', 3))}))
        with patch.object(war_reads, 'selected_wars', AsyncMock(return_value={'rows': rows, 'war_ids': ['one']})):
            result = await war_reads.attendance(context, {'war_type': 'REG', 'unit': 'member',
                'clan_codes': ['BE1', 'BEE', 'BE4'], 'top_n': 1})
        visible = answer_evidence({'wars': result})['wars']
        first, second, empty = visible['clan_participation']
        self.assertEqual(first['warring']['member_count'], 1)
        self.assertEqual(first['warring']['unlinked_account_count'], 1)
        self.assertEqual(first['joined_without_attacking']['member_count'], 1)
        self.assertEqual(first['unknown']['member_count'], 1)
        self.assertEqual(first['joined_without_attacking']['identities'][0]['member_id'], 2)
        self.assertEqual(second['warring']['member_count'], 1)
        self.assertEqual(empty['wars_observed'], 0)
        self.assertEqual(len(result['rows']), 1)  # summaries precede top-N transport selection

    def test_positive_partial_attacks_are_known_but_partial_zero_is_unknown(self):
        self.assertEqual(war_reads.participation_status([{'attacks_used': 1, 'missed': None}]), 'warring')
        self.assertEqual(war_reads.participation_status([{'attacks_used': 0, 'missed': None}]), 'unknown')
        self.assertEqual(war_reads.participation_status([]), 'unknown')
