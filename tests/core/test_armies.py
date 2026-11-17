"""An unmapped game ID must remain unknown, with its quantity intact."""

import unittest

from elbow_helper.domain.armies import decode_army


class ArmyUnknownTests(unittest.TestCase):
    def test_host_unknown_troop_keeps_its_id_and_count(self):
        self.assertEqual(decode_army('u12x90-2x1'), {'troops': {
            'unknown_90': {'unknown': True, 'id': 90, 'count': 12}, 'Archer': 2}})
