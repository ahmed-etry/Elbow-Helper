from pathlib import Path
import unittest

from elbow_helper.features.rosters.repository import RosterRepository
from tests.temp_directory import TemporaryDirectory


class ScheduledCarryTests(unittest.TestCase):
    def test_existing_target_signup_is_preserved_when_accounts_carry_over(self):
        with TemporaryDirectory() as temporary:
            repository = RosterRepository(Path(temporary) / 'rosters.sqlite3')
            roster = repository.create_roster(
                guild_id=1, name='Carry', clan_code='BEM', role_id=None, max_members=10,
            )
            repository.update_roster(roster.id, reset_on_open=0)
            target, _ = repository.start_cycle(roster.id, 'target')
            existing = dict(player_tag='#P0', player_name='Existing', townhall=16)
            repository.add_members(roster.id, target.active_cycle_id, 1, [existing], 10)
            before = repository.list_members(roster.id, target.active_cycle_id)[0]
            previous, _ = repository.start_cycle(roster.id, 'previous')
            repository.add_members(roster.id, previous.active_cycle_id, 2, [
                {**existing, 'player_name': 'Carried'},
                dict(player_tag='#Q0', player_name='Other', townhall=15),
            ], 10)

            opened, previous_id = repository.start_scheduled_cycle(roster.id, 'target')

            self.assertEqual(previous_id, previous.active_cycle_id)
            self.assertEqual(opened.active_cycle_id, target.active_cycle_id)
            self.assertEqual(opened.status, 'open')
            members = repository.list_members(roster.id, opened.active_cycle_id)
            self.assertEqual([member.player_tag for member in members], ['#P0', '#Q0'])
            self.assertEqual(members[0], before)
            self.assertEqual(members[1].discord_user_id, 2)
