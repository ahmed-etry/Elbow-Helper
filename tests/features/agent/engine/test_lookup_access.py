"""Read access follows the command catalogue and survives retained reports."""

from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE, RECRUITERS
from elbow_helper.features.agent.access import ACCESS_ROLE_SETS, AgentAccessLost, has_access_requirements
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.agent.wording import ACTION_UNAVAILABLE


class LookupAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_levels_equal_help_access_groups(self):
        mirrors = {
            "lead": ("/event list",),
            "lead_plus": ("/raffle list", "/raffle history", "/health player", "/health clan"),
            "core": ("/coinlog",),
            "recruiter_or_core": ("/account list",),
            "lead_plus_or_cwl_helper": ("/cwl bonus",),
        }
        entries = {entry.path: entry for entry in HELP_ENTRIES}
        for level, paths in mirrors.items():
            for path in paths:
                with self.subTest(level=level, path=path):
                    self.assertEqual(ACCESS_ROLE_SETS[level], entries[path].visible_to)

    def test_departments_are_any_role_but_combined_levels_are_all_required(self):
        member = SimpleNamespace(roles=[])
        guild = SimpleNamespace(get_member=lambda _: member)
        for level, identifiers in ACCESS_ROLE_SETS.items():
            for identifier in identifiers:
                member.roles = [SimpleNamespace(id=identifier)]
                self.assertTrue(has_access_requirements(guild, 2, {level}))
            member.roles = []
            self.assertFalse(has_access_requirements(guild, 2, {level}))
        member.roles = [SimpleNamespace(id=next(iter(RECRUITERS)))]
        self.assertFalse(has_access_requirements(guild, 2, {"recruiter_or_core", "core"}))
        member.roles.append(SimpleNamespace(id=next(iter(CORE))))
        self.assertTrue(has_access_requirements(guild, 2, {"recruiter_or_core", "core"}))

    async def test_new_lookup_denial_is_local_and_retained_access_loss_still_aborts(self):
        member = SimpleNamespace(id=2, roles=[])
        context = SimpleNamespace(member=member, guild=SimpleNamespace(get_member=lambda _: member),
                                  state=AgentTurnState(), source_message=SimpleNamespace(id=7))
        handler = AsyncMock(return_value={"value": 1})
        with patch("elbow_helper.features.agent.engine.tool_call.require_evidence_access", new=AsyncMock()):
            result = await AgentService.execute_tool(name="synthetic", handler=handler, arguments={},
                capability_scope={"required_access": ["core"]}, context=context)
        self.assertEqual(json.loads(result), {"error": ACTION_UNAVAILABLE, "required_access": ["core"]})
        handler.assert_not_awaited()
        with patch("elbow_helper.features.agent.engine.tool_call.require_evidence_access",
                   new=AsyncMock(side_effect=AgentAccessLost())):
            with self.assertRaises(AgentAccessLost):
                await AgentService.execute_tool(name="synthetic", handler=handler, arguments={},
                    capability_scope={"required_access": ["core"]}, context=context)
