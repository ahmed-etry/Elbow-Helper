"""War participation separates attacks, confirmed non-participation and unknowns."""
import unittest

from elbow_helper.agent.reads import war_tools as war_reads


class ParticipationCountsTests(unittest.IsolatedAsyncioTestCase):
    def test_positive_partial_attacks_are_known_but_partial_zero_is_unknown(self):
        self.assertEqual(war_reads.participation_status([{'attacks_used': 1, 'missed': None}]), 'warring')
        self.assertEqual(war_reads.participation_status([{'attacks_used': 0, 'missed': None}]), 'unknown')
        self.assertEqual(war_reads.participation_status([]), 'unknown')
