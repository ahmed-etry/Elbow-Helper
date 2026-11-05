"""Collected signup cohorts remain separate from actual CWL battle lineups."""

import unittest

from features.test_cwl_record import LeagueFixture, NOV, SECOND, now
from tests.data.test_wars import group


class CwlCohortTests(LeagueFixture, unittest.IsolatedAsyncioTestCase):
    async def test_checked_group_returns_only_own_signups_including_benched_players(self):
        payload = group(season='2026-11')
        payload['clans'][0]['members'].append({'tag': '#P8', 'name': 'Bench', 'townHallLevel': 18})
        await self.record(payload, kind='cwl_group', at=NOV)
        with now(NOV + 60 * SECOND):
            result = await self.reads.cohort('#PY', season='2026-11')
        self.assertEqual(result, {'player_tags': ['#P0', '#P8'], 'season': '2026-11', 'observed_at': NOV})
        projected = await self.call(self.history.seasonal_cohort, clan_code='ONE', season='2026-11',
                                    at=NOV + 60 * SECOND)
        self.assertEqual(projected['player_tags'], ['#P0', '#P8'])
        self.assertEqual(projected['observed_at'], '2026-11-02T08:00:00+00:00')

    async def test_missing_wrong_season_stale_or_failed_groups_never_supply_a_signup_cohort(self):
        with now(NOV):
            self.assertIsNone(await self.reads.cohort('#PY'))
        await self.record(group(season='2026-11'), kind='cwl_group', at=NOV)
        with now(NOV + 60 * SECOND):
            self.assertIsNone(await self.reads.cohort('#PY', season='2026-10'))
        with now(NOV + 86400 * SECOND):
            self.assertIsNone(await self.reads.cohort('#PY'))
        await self.record(None, kind='cwl_group', status=503, at=NOV + 120 * SECOND)
        with now(NOV + 130 * SECOND):
            self.assertIsNone(await self.reads.cohort('#PY'))
