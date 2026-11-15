"""A name lookup asks each question once, however many access checks it makes."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads import account_names, identity_reads
from elbow_helper.agent.safety import disclosure
from elbow_helper.agent.safety.access import reader_clearance, has_reader_access_requirements, KNOWN_ACCESS_REQUIREMENTS
from elbow_helper.configuration.roles import CORE, CO_LEADER_ROLE_ID, MEMBER_ROLE_ID, SLEEPING_CO_ROLE_ID
from tests.agent.reads.test_named_account_stats import lookup_context, named_member, rosters


class Reader:
    """The requester, who sits in the destination audience; counts how often roles are rebuilt."""

    def __init__(self, member):
        self.id, self.display_name, self.bot = member.id, member.display_name, False
        self.role_ids, self.role_reads = [next(iter(CORE))], 0

    @property
    def roles(self):
        self.role_reads += 1
        return [SimpleNamespace(id=identifier) for identifier in self.role_ids]


def linked_context(rows):
    context = lookup_context([named_member(42, 'Different Pilot')], [(tag, name, 42) for tag, name in rows])
    actor = Reader(context.member)
    members = [actor, *context.guild.members[1:]]
    context.guild.members = members
    context.guild.get_member = lambda identifier: next((m for m in members if m.id == identifier), None)
    people = SimpleNamespace(
        link_service=object(),
        current_links=AsyncMock(return_value={'coverage': {}, 'rows': [
            {'player_tag': tag, 'player_name_last_seen': name, 'discord_user_id': 42} for tag, name in rows]}),
        directory_names=AsyncMock(return_value={'rows': []}),
        resolve=AsyncMock(return_value={'rows': [], 'next_offset': None}))
    return replace(context, member=actor, bot=SimpleNamespace(people_reads=people)), actor, people


class LookupRepeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_name_lookup_reads_links_and_roles_once_per_check(self):
        context, actor, people = linked_context([('#P0', 'Silver Kite'), ('#Q0', 'Other Beacon')])
        scopes = []
        real = disclosure.request_scope

        async def counting(context, **kwargs):
            scopes.append(kwargs)
            return await real(context, **kwargs)

        with patch.object(account_names, 'read_clash', AsyncMock(return_value=rosters())), \
             patch.object(identity_reads, 'profiles', AsyncMock(return_value={})), \
             patch.object(disclosure, 'request_scope', counting):
            result = await identity_reads.member_accounts(context, {'name': 'Silver Kite', 'look_in': 'game'})
        self.assertEqual([row['player_tag'] for row in result['rows']], ['#P0'])
        # Each link selection filters one table, so the table is read once for the lookup.
        people.current_links.assert_awaited_once()
        # A check rebuilds the requester's sorted roles a few times, never once per access level.
        refreshes = sum(1 for kwargs in scopes if kwargs.get('refresh'))
        self.assertGreater(refreshes, 5)
        self.assertLess(actor.role_reads, len(KNOWN_ACCESS_REQUIREMENTS) * refreshes)

    async def test_clearance_matches_checking_each_level_separately(self):
        for roles in ([], [MEMBER_ROLE_ID], [CO_LEADER_ROLE_ID], [SLEEPING_CO_ROLE_ID], [next(iter(CORE))],
                      [MEMBER_ROLE_ID, CO_LEADER_ROLE_ID]):
            actor = Reader(SimpleNamespace(id=7, display_name='Synthetic'))
            actor.role_ids = roles
            guild = SimpleNamespace(get_member=lambda identifier: actor if identifier == 7 else None, me=None)
            expected = frozenset(level for level in KNOWN_ACCESS_REQUIREMENTS
                                 if has_reader_access_requirements(guild, 7, {level}))
            actor.role_reads = 0
            self.assertEqual(reader_clearance(guild, 7), expected, roles)
            self.assertEqual(actor.role_reads, 1)
        self.assertEqual(reader_clearance(guild, 8), frozenset())


if __name__ == '__main__':
    unittest.main()
