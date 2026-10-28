from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.discord_actions.safety import (
    DiscordActionRefused, managed_role_commands, member_role_owner,
)
from elbow_helper.features.hibernation import config as hibernation_config
from elbow_helper.features.hibernation.queries import HibernationQueries
from elbow_helper.features.recruitment import config as recruitment_config
from elbow_helper.features.recruitment.queries import RecruitmentQueries


class RoleOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        settings = {
            "elbow_helper.agent.discord_actions.safety": {
                "MEMBER_ROLE_ID": 11, "ALLIANCE_MEMBER_ROLE_ID": 12,
                "CLAN_MEMBER_ROLE_IDS": {"FAKE": 13},
                "HIBERNATE_REMOVE_ROLE_IDS": {11, 61},
                "RESTORE_ROLE_IDS": (62,), "SNAPSHOT_ONLY_ROLE_IDS": (63,),
            },
            "elbow_helper.features.recruitment.config": {
                "APPLICANT_ROLE_ID": 21, "TRIAL_ROLE_ID": 22,
            },
            "elbow_helper.features.hibernation.config": {
                "HIBERNATING_ROLE_ID": 31, "SLEEPING_CO_ROLE_ID": 32,
                "SLEEPING_CROSS_ROLE_ID": 33,
            },
        }
        for module, values in settings.items():
            for name, value in values.items():
                self.patches.enter_context(patch(module + "." + name, value))
        self.patches.enter_context(patch(
            "elbow_helper.agent.discord_actions.safety.war_roles",
            return_value={41},
        ))
        self.trials = {"100": {"applicant_id": 4}}
        self.hibernation = {"5": {}}
        self.context = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            recruitment_queries=RecruitmentQueries(lambda: self.trials),
            hibernation_queries=HibernationQueries(lambda: self.hibernation),
            roster_queries=SimpleNamespace(managed_role_ids=AsyncMock(return_value={42})),
            role_connection_queries=SimpleNamespace(managed_role_ids=lambda: {51}),
        )

    async def test_only_lifecycle_roster_and_connection_roles_are_always_owned(self):
        self.assertEqual(recruitment_config.managed_role_ids(), {21, 22})
        self.assertEqual(hibernation_config.managed_role_ids(), {31, 32, 33})
        self.assertEqual(await managed_role_commands(self.context), {
            21: "/accept", 22: "/accept", 31: "/hibernate", 32: "/hibernate",
            33: "/hibernate", 41: "/roster", 42: "/roster", 51: "/connections",
        })

    async def test_membership_roles_are_owned_per_member_and_rechecked_from_state(self):
        for role_id in (11, 12, 13):
            self.assertEqual(member_role_owner(self.context, role_id, 4), "trial")
            self.assertEqual(member_role_owner(self.context, role_id, 5), "hibernation")
            self.assertIsNone(member_role_owner(self.context, role_id, 6))
        self.trials.clear()
        self.hibernation.clear()
        self.assertIsNone(member_role_owner(self.context, 11, 4))
        self.assertIsNone(member_role_owner(self.context, 13, 5))

    async def test_saved_roles_are_owned_only_for_hibernating_members(self):
        for role_id in (61, 62, 63):
            self.assertEqual(member_role_owner(self.context, role_id, 5), "hibernation")
            self.assertIsNone(member_role_owner(self.context, role_id, 4))
            self.assertIsNone(member_role_owner(self.context, role_id, 6))
        self.hibernation.clear()
        for role_id in (61, 62, 63):
            self.assertIsNone(member_role_owner(self.context, role_id, 5))

    async def test_missing_state_does_not_authorize_membership_role_changes(self):
        self.context.recruitment_queries = None
        with self.assertRaises(DiscordActionRefused):
            member_role_owner(self.context, 11, 4)
        self.assertIsNone(member_role_owner(self.context, 99, 4))
