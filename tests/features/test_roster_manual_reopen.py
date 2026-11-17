import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.repository import RosterRepository
from tests.features.test_rosters import _wire_roster_service
from tests.temp_directory import TemporaryDirectory


class ManualReopenTests(unittest.IsolatedAsyncioTestCase):
    async def test_hand_closed_scheduled_roster_reopens_in_its_window_and_closes_on_time(self):
        now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        for once in (False, True):
            with self.subTest(one_off=once), TemporaryDirectory() as temporary:
                repo = RosterRepository(Path(temporary) / 'rosters.sqlite3')
                roster = repo.create_roster(guild_id=1, name='Practice signup', clan_code='BEM',
                                            role_id=None, max_members=10)
                repo.configure_schedule(roster.id, enabled=not once, timezone_name='UTC',
                                        open_day='10', open_time='08:00', close_day='28', close_time='08:00',
                                        reset_on_open=True)
                if once:
                    repo.update_roster(roster.id,
                        one_off_open_ts=int((now - timedelta(days=1)).timestamp()),
                        one_off_close_ts=int((now + timedelta(days=1)).timestamp()))
                cog = object.__new__(Rosters)
                _, automation, _, _ = _wire_roster_service(cog, repo)
                await automation.run_due(now)
                opened = repo.get_roster(roster.id)
                repo.add_members(roster.id, opened.active_cycle_id, 123,
                                 [dict(player_tag='#P0', player_name='Spruce', townhall=16)], 10)
                await cog.change_roster_management(roster.id, 'close')
                await automation.run_due(now)
                self.assertEqual(repo.get_roster(roster.id).status, 'closed')
                with patch('elbow_helper.features.rosters.services.automation.datetime') as clock:
                    clock.now.return_value = now
                    clock.fromtimestamp.side_effect = datetime.fromtimestamp
                    reply = await cog.change_roster_management(roster.id, 'open')
                self.assertEqual(reply, 'Opened **Practice signup**.')
                reopened = repo.get_roster(roster.id)
                self.assertEqual(reopened.active_cycle_id, opened.active_cycle_id)
                self.assertEqual(len(repo.list_members(roster.id, reopened.active_cycle_id)), 1)
                self.assertEqual(reopened.schedule_enabled, not once)
                self.assertIsNone(repo.snapshot(1, roster.id).cycle.closed_ts)
                await automation.run_due(now + timedelta(days=14))
                self.assertEqual(repo.get_roster(roster.id).status, 'closed')
                await automation.run_due(datetime(2026, 12, 10, 9, tzinfo=timezone.utc))
                self.assertEqual(repo.get_roster(roster.id).status, 'closed' if once else 'open')
