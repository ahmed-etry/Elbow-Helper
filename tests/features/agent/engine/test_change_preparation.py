"""Scripted change requests must deliver real previews or explain missing inputs."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.actions.outcomes import ActionOutcome
from elbow_helper.features.agent.actions.preview import ConfirmationView, preview_text
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.engine.budgets import RequestLimits
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.engine.change_answers import unprepared_change_issue
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.wording import ACTION_NOT_PREPARED
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


class ProtocolSession(_Session):
    """Reject unmatched tool messages just as the provider does."""

    def __init__(self, steps):
        super().__init__(steps, [])
        self.pending = set()

    async def advance(self, results=(), **kwargs):
        identifiers = [result.call_id for result in results]
        if len(identifiers) != len(set(identifiers)) or set(identifiers) != self.pending:
            raise AssertionError(f"Pending calls {self.pending}; supplied results {identifiers}")
        step = await super().advance(results, **kwargs)
        self.pending = {call.call_id for call in step.tool_calls}
        return step


def planned(capability, arguments, *, identity="change", self_target=False, reply=None):
    plan = _plan([{"id": identity, "capability": capability,
                   "arguments": arguments, "depends_on": []}])
    plan["self_target"] = self_target
    if reply is not None:
        plan["reply"] = reply
    return _model_step(plan)


def text(value):
    return AgentStep(value, (), AgentUsage(1000, 100))


class ChangePreparationTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = _context()
        self.context.member.mention = "<@41>"
        self.context.member.bot = False
        self.context.source_message.guild = self.context.guild
        self.context.source_message.author = self.context.member
        self.context.source_message.mentions = ()
        self.context.source_message.archive_reply = False
        self.context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=501))
        self.workflow = SimpleNamespace()
        self.context = replace(self.context, bot=SimpleNamespace(
            tree=object(), user=self.context.guild.me, get_cog=lambda name: self.workflow,
        ))
        self.registry = build_agent_tools()

    async def answer(self, session, question, *, limits=None):
        with (
            patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=self.registry),
            patch("elbow_helper.features.agent.engine.service.build_command_tools", return_value=({}, {})),
            patch("elbow_helper.features.agent.engine.budgets.RESEARCH_LIMITS",
                  limits or RequestLimits(10, 500_000, 100_000, 8192)),
        ):
            return await AgentService(_Model(session)).answer(
                question=question, local_context="", context=self.context,
            )

    async def deliver(self, answer):
        delivery = AgentDeliveryMixin()
        delivery.bot = self.context.bot
        delivery.action_runner = SimpleNamespace(submit=AsyncMock())
        await delivery.send_response(self.context.source_message, answer, None, context=self.context)
        view = self.context.source_message.reply.await_args.kwargs["view"]
        self.assertIsInstance(view, ConfirmationView)
        self.assertEqual(view.proposals, tuple(self.context.state.proposed_changes))
        self.assertEqual(view.owner_id, 41)
        self.assertIn("Confirm", [item.label for item in view.children])
        self.assertEqual(self.context.source_message.reply.await_args.args[0], answer)
        delivery.action_runner.submit.assert_not_awaited()
        view.stop()

    def roster(self, *, missing=False):
        roster = SimpleNamespace(id=4, name="BEH CWL", guild_id=3, role_id=None)
        account = SimpleNamespace(player_name="Main", player_tag="#P0")
        picker = SimpleNamespace(accounts=[] if missing else [account], message="Choose your main account.")
        self.workflow.prepare_roster_account_selection = AsyncMock(return_value=(roster, picker))
        self.workflow.resolve_roster_account_choices = Mock(return_value=(["#P0"], ""))
        self.workflow.roster_edit_state = AsyncMock(return_value={"posts": []})
        self.workflow.change_roster_accounts = AsyncMock()
        self.registry["read_target"] = RegisteredAgentTool(AgentToolDefinition(
            "read_target", "Resolve the requested roster and main account.",
            {"type": "object", "properties": {}},
        ), AsyncMock(return_value={"roster_id": 4, "accounts": ["#P0"]}),
            contract=CapabilityContract(()))

    async def test_roster_confirmation_claim_replans_into_a_real_preview(self):
        self.roster()
        session = ProtocolSession([
            planned("read_target", {}, identity="target"),
            text("Confirm and I'll add #P0 to BEH CWL."),
            planned("signup_roster_accounts", {"roster_id": 4, "accounts": ["#P0"]}),
            text("Your main account is #P0."),
        ])
        answer = await self.answer(session, "Add me to the BEH CWL roster with my main")
        self.assertEqual(len(self.context.state.proposed_changes), 1)
        action = self.context.state.proposed_changes[0]
        self.assertEqual(action.capability_name, "signup_roster_accounts")
        self.assertEqual(action.values, {"roster_id": 4, "member_id": 41, "accounts": ["#P0"]})
        self.assertIn("No change has been prepared", session.instructions[2])
        self.assertIn("BEH CWL", self.context.state.preview_reply)
        self.assertEqual(answer, "Your main account is #P0.")
        self.workflow.change_roster_accounts.assert_not_awaited()

    async def test_direct_confirmation_text_cannot_substitute_for_roster_preparation(self):
        self.roster()
        session = ProtocolSession([
            text("Confirm and I'll add #P0 to BEH CWL."),
            planned("signup_roster_accounts", {"roster_id": 4, "accounts": ["#P0"]}),
        ])
        answer = await self.answer(session, "Add my main #P0 to BEH CWL")
        self.assertEqual(answer, preview_text(self.context.state.proposed_changes))
        await self.deliver(answer)
        self.workflow.change_roster_accounts.assert_not_awaited()
        self.assertEqual(len(session.calls), 2)

    async def test_missing_roster_input_is_explained_without_a_confirmation(self):
        self.roster(missing=True)
        session = ProtocolSession([
            planned("signup_roster_accounts", {"roster_id": 4, "accounts": ["#P0"]}),
            text("Which linked account is your main?"),
        ])
        answer = await self.answer(session, "Add me to the BEH CWL roster with my main")
        self.assertEqual(answer, "Which linked account is your main?")
        self.assertEqual(self.context.state.proposed_changes, [])
        self.workflow.change_roster_accounts.assert_not_awaited()

    async def test_answer_only_round_replaces_unprepared_confirmation_with_missing_input(self):
        self.roster()
        session = ProtocolSession([
            planned("read_target", {}, identity="target"),
            text("Confirm and I'll add #P0 to BEH CWL."),
            text("I still need to know which linked account is your main."),
        ])
        with patch("elbow_helper.features.agent.engine.budgets.MAX_MODEL_ROUNDS", 2):
            answer = await self.answer(session, "Add me to BEH CWL")
        self.assertFalse(session.calls[1][1])
        self.assertFalse(session.calls[2][1])
        self.assertEqual(answer, "I still need to know which linked account is your main.")
        self.assertEqual(self.context.state.proposed_changes, [])

    async def test_repeated_confirmation_claim_has_a_bounded_honest_fallback(self):
        self.roster()
        session = ProtocolSession([
            planned("read_target", {}, identity="target"),
            text("Confirm and I'll add #P0 to BEH CWL."),
            text("Please confirm."),
        ])
        with patch("elbow_helper.features.agent.engine.budgets.MAX_MODEL_ROUNDS", 2):
            answer = await self.answer(session, "Add me to BEH CWL")
        self.assertEqual(answer, ACTION_NOT_PREPARED)
        self.assertEqual(self.context.state.proposed_changes, [])
        self.assertEqual(len(session.calls), 3)

    def reminder(self):
        repository = Mock()
        repository.member_timezone.return_value = None
        self.context = replace(self.context, action_repository=repository)
        return {
            "kind": "reminder", "text": "Check the war.", "deliver_to": "dm",
            "dm_member_ids": [41], "schedule": {
                "kind": "once", "at_utc": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
        }

    async def test_reminder_preview_reaches_member_without_an_extra_model_round(self):
        values = self.reminder()
        session = ProtocolSession([planned("save_standing_rule", values, self_target=True)])
        answer = await self.answer(session, "Remind me in 2 hours to check the war")
        self.assertEqual(answer, preview_text(self.context.state.proposed_changes))
        self.assertIn("Set reminder", answer)
        self.assertIn("Check the war.", answer)
        self.assertEqual(len(session.calls), 1)
        await self.deliver(answer)
        self.context.action_repository.create_standing.assert_not_called()

    async def test_recorded_reminder_plan_corrections_preserve_provider_protocol(self):
        values = self.reminder()
        session = ProtocolSession([
            planned("save_standing_rule", values, reply="Confirm and it's locked in."),
            planned("save_standing_rule", values, self_target=True),
        ])
        answer = await self.answer(session, "Remind me in 2 hours to check the war")
        refusal = json.loads(session.calls[1][0][0].content)["results"]["change"]
        self.assertIn("self_target=true", refusal["error"])
        self.assertEqual(len(self.context.state.proposed_changes), 1)
        self.assertEqual(len(session.calls), 2)
        await self.deliver(answer)
        self.context.action_repository.create_standing.assert_not_called()

    async def test_late_member_repair_supplies_the_pending_plan_result(self):
        async def output(context, arguments):
            context.state.outcomes.append(ActionOutcome("complete", text="<@42> result"))
            return {"status": "complete"}

        self.registry["synthetic_output"] = RegisteredAgentTool(AgentToolDefinition(
            "synthetic_output", "Produce a synthetic outcome.", {"type": "object", "properties": {}},
        ), output, AgentCapabilityEffect.COMMAND, ActionClass.OUTPUT, contract=CapabilityContract(()))
        session = ProtocolSession([
            planned("synthetic_output", {}), text("Which member did you mean?"),
        ])
        answer = await self.answer(session, "Produce the synthetic outcome")
        self.assertEqual(answer, "Which member did you mean?")
        self.assertEqual(json.loads(session.calls[1][0][0].content)["results"]["change"]["status"], "complete")


class ConfirmationClaimTests(unittest.TestCase):
    def test_execution_invitations_require_prepared_changes(self):
        for answer in ("Confirm and I'll add #P0.", "Click **Confirm** to proceed.",
                       "**Confirm**, and the account will be added.",
                       "Once you approve, I'll schedule it.", "Waiting for your confirmation.",
                       "Please confirm.", "I'll remove that account."):
            with self.subTest(answer=answer):
                self.assertTrue(unprepared_change_issue(answer, []))
                self.assertFalse(unprepared_change_issue(answer, [object()]))

    def test_missing_inputs_facts_and_capability_descriptions_are_allowed(self):
        for answer in ("Which account is your main?", "Can you confirm which account you mean?",
                       "I can add linked accounts to an open roster.", "The roster is open.",
                       "CWL eligibility requires leadership approval.",
                       "> Confirm and I'll add #P0.\nThat was the earlier reply.",
                       "Example message:\n```\nI'll send the reminder.\n```",
                       "I couldn't prepare that change for confirmation."):
            with self.subTest(answer=answer):
                self.assertFalse(unprepared_change_issue(answer, []))
