"""GAPS-FIX-2 regression: RoleMeaningTests."""
import json
from types import SimpleNamespace
import unittest
from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.features.agent.families.reference_reads import community_reference
from elbow_helper.features.agent.plan.format import answer_instructions
from features.agent.engine.test_agent_plan_flow import _context


class RoleMeaningTests(unittest.IsolatedAsyncioTestCase):
    async def test_elder_display_name_cannot_override_configured_role_scope(self):
        context = _context()
        # Misleading display names must never supply a clan or an elder policy.
        renamed = SimpleNamespace(id=CLANS['BES'].leadership_role_id, name='Elder BEH')
        unconfigured = SimpleNamespace(id=999, name='Elder BES')
        context.guild.roles.extend([renamed, unconfigured])
        clans = await community_reference(context, {'reference_kind': 'clans', 'clan_codes': ['BES']})
        roles = clans['rows'][0]['roles']
        binding = next(role for role in roles if role['role_id'] == renamed.id)
        self.assertEqual(binding['purpose'], 'leadership')
        self.assertEqual(binding['name'], 'Elder BEH')
        self.assertNotIn(unconfigured.id, [role['role_id'] for role in roles])
        meanings = await community_reference(context, {'reference_kind': 'roles'})
        self.assertNotIn('Elder BEH', json.dumps(meanings))
        for instructions in (FAMILY_INSTRUCTION, answer_instructions(),
                             answer_instructions(build_agent_tools())):
            self.assertIn("Never infer a role's meaning", instructions)
            self.assertIn('unknown', instructions)
            self.assertIn('server role staff', instructions)
