"""Synthetic rank contradictions, ambiguity and one bounded repair."""

import unittest

from elbow_helper.features.agent.engine.role_claims import role_conflicts
from elbow_helper.features.agent.engine.budgets import RequestLimits
from features.agent.engine import test_request_cost as cost
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from unittest.mock import AsyncMock


def profiles(compact=False):
    items = [{"tag": "#P0", "status": "ok", "data": {"name": "North", "role": "coLeader"}},
             {"tag": "#P2", "status": "ok", "data": {"name": "North2", "role": "admin"}}]
    return {"source": "clash_api", "kind": "player", "items": (
        {"columns": ["tag", "status", "data"], "rows": [[r[k] for k in ("tag", "status", "data")] for r in items]}
        if compact else items)}


class RoleClaimTests(unittest.TestCase):
    def test_explicit_rank_mismatch_and_faithful_rank_labels(self):
        for compact in (False, True):
            evidence = {"profiles": profiles(compact)}
            self.assertEqual(role_conflicts("**North** (#P0), TH18, elder.", evidence),
                             ("#P0 in-game role: co-leader.",))
            self.assertEqual(role_conflicts("North is co-leader.\nNorth2 is elder.", evidence), ())
            self.assertEqual(role_conflicts("North2 is co-leader.", evidence),
                             ("#P2 in-game role: elder.",))

    def test_discord_roles_history_proposals_and_ambiguous_subjects_are_not_repaired(self):
        evidence = {"profiles": profiles()}
        for answer in ("North has the Discord elder role.", "North was an elder.",
                       "North should be elder.", "North is not elder.",
                       "> North is elder.", "North and North2 are elders."):
            self.assertEqual(role_conflicts(answer, evidence), (), answer)
        conflicting = profiles()
        conflicting["items"][0]["data"]["role"] = "leader"
        self.assertEqual(role_conflicts("North is elder.", {**evidence, "other": conflicting}), ())
        self.assertEqual(role_conflicts("North is elder.", {"messages": {"content": profiles()}}), ())
        self.assertEqual(role_conflicts("North is elder.", {"failed": {
            **profiles(), "flags": {"status": "refused"}}}), ())

    def test_clan_roster_can_verify_rank_without_player_lookup(self):
        result = {"source": "clash_api", "kind": "clan", "items": [{"tag": "#Q0", "status": "ok",
                  "data": {"memberList": [{"tag": "#P0", "name": "North", "role": "leader"}]}}]}
        self.assertEqual(role_conflicts("North is elder.", {"clan": result}),
                         ("#P0 in-game role: leader.",))

    def test_account_names_are_not_rank_claims(self):
        result = profiles()
        result["items"][0]["data"]["name"] = "Elder North"
        self.assertEqual(role_conflicts("Elder North (#P0), TH18.", {"profiles": result}), ())
        self.assertEqual(role_conflicts("Elder North (#P0) is elder.", {"profiles": result}),
                         ("#P0 in-game role: co-leader.",))


class RoleRepairTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_one_repair_uses_typed_rank_and_retains_account_scope(self):
        tool = RegisteredAgentTool(AgentToolDefinition("read_clash", "Read profiles.", {
            "type": "object", "properties": {}}), AsyncMock(return_value=profiles()))
        flow, session, _, _ = self.flow([cost.plan(), cost.response("North is elder."),
                                        cost.response("North is co-leader; North2 is elder.")],
                                       registry={"read_clash": tool})
        self.assertEqual(await flow.run(), "North is co-leader; North2 is elder.")
        self.assertIn("#P0 in-game role: co-leader", session.instructions[-1])
        self.assertEqual(len(session.calls), 3)

    async def test_no_remaining_repair_budget_preserves_verified_role(self):
        tool = RegisteredAgentTool(AgentToolDefinition("read_clash", "Read profiles.", {
            "type": "object", "properties": {}}), AsyncMock(return_value=profiles()))
        flow, session, _, _ = self.flow([cost.plan(), cost.response("North is elder.")],
                                       registry={"read_clash": tool})
        flow.budget.request_limits = RequestLimits(2, 80_000, 12_000, 4096)
        self.assertIn("#P0 in-game role: co-leader", await flow.run())
        self.assertEqual(len(session.calls), 2)
