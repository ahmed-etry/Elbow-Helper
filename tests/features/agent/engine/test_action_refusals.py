"""Refused changes explain their result before another confirmation is offered."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.actions import contracts
from elbow_helper.features.agent.actions.contracts import ActionClass, ChangePreview, PreparedAction
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.scheduled.tools import prepare_save
from elbow_helper.features.agent.capabilities.records.commands import _edit_target
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan, _step, _model_step


class ActionRefusalTests(unittest.IsolatedAsyncioTestCase):
    def test_record_input_validation_preserves_the_feature_refusal(self):
        service = SimpleNamespace(validate_details=MagicMock(
            side_effect=ValueError("Add details about what happened.")))
        record = {"category_key": "synthetic", "incident_type_key": "synthetic", "note": ""}
        with self.assertRaisesRegex(contracts.ActionRefused, "Add details about what happened"):
            _edit_target(service, record, {})

    async def test_safe_refusal_reaches_tool_result_without_traceback(self):
        async def refuse(context, arguments):
            raise contracts.ActionRefused("Synthetic target unavailable")
        with patch("elbow_helper.features.agent.engine.tool_call.LOGGER.exception") as diagnostic:
            result = await AgentService.execute_tool(
                name="synthetic_change", handler=refuse, arguments={}, context=_context(),
                action_class=ActionClass.CHANGE,
            )
        self.assertEqual(json.loads(result), {"error": "Synthetic target unavailable"})
        diagnostic.assert_not_called()

    async def test_unexpected_value_error_is_logged_and_masked(self):
        async def fail(context, arguments):
            raise ValueError("Synthetic internal diagnostic")
        with patch("elbow_helper.features.agent.engine.tool_call.LOGGER.exception") as diagnostic:
            result = await AgentService.execute_tool(
                name="read_value", handler=fail, arguments={}, context=_context(),
            )
        self.assertEqual(json.loads(result), {"error": "That lookup failed."})
        diagnostic.assert_called_once()

    async def test_partial_change_results_offer_the_rest_without_a_preview(self):
        for status in ("refused", "no_change", "needs_input"):
            with self.subTest(status=status):
                run = AsyncMock()
                async def prepare(context, arguments):
                    if arguments["target"] == 202:
                        if status == "refused":
                            raise contracts.ActionRefused("Synthetic target unavailable")
                        return {"status": status, "missing": ["timezone"]}
                    context.state.proposed_changes.append(PreparedAction(
                        "synthetic_change", dict(arguments),
                        ChangePreview(("Change target 101",), AsyncMock(return_value=True)), run,
                    ))
                    return {"status": "confirmation_required"}
                tool = RegisteredAgentTool(AgentToolDefinition(
                    "synthetic_change", "Change a target.",
                    {"type": "object", "properties": {"target": {"type": "integer"}},
                     "required": ["target"], "additionalProperties": False},
                ), prepare, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE)
                plan = _plan([{**_step(str(target), {"target": target}),
                               "capability": "synthetic_change"} for target in (101, 202)])
                session = _Session([_model_step(plan), AgentStep("Explain changes.", (), AgentUsage())], [])
                context = replace(_context(), bot=SimpleNamespace(tree=object()))
                with (patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                            return_value={"synthetic_change": tool}),
                      patch("elbow_helper.features.agent.engine.service.build_command_tools",
                            return_value=({}, {}))):
                    answer = await AgentService(_Model(session)).answer(
                        question="Change both targets", local_context="", context=context,
                    )
                self.assertEqual(answer, "Explain changes.")
                self.assertEqual(context.state.proposed_changes, [])
                run.assert_not_awaited()
                feedback = json.loads(session.calls[-1][0][0].content)
                self.assertEqual(session.calls[-1][1], status != "needs_input")
                if status == "needs_input":
                    self.assertIn("timezone", feedback["missing_hints"])
                else:
                    self.assertEqual(feedback["instruction"],
                        "Answer now from these results. "
                        "Submit another plan only for a remaining gap.")
                if status == "refused":
                    self.assertEqual(feedback["results"]["202"]["error"], "Synthetic target unavailable")

    async def test_schedule_only_asks_for_a_timezone_for_local_rules(self):
        for kind in ("weekly", "monthly", "once", "interval"):
            with self.subTest(kind=kind):
                repository = SimpleNamespace(member_timezone=lambda _: None,
                    create_standing=MagicMock(), set_member_timezone=MagicMock())
                context = replace(_context(), action_repository=repository)
                future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                schedule = {"kind": kind, "time": "12:00", "days": [1],
                            "at_utc": future, "anchor_utc": future, "seconds": 3600}
                values = {"kind": "request", "request": "Synthetic request",
                          "schedule": schedule, "destination_channel_id": 91}
                channel = SimpleNamespace(id=91, mention="<#91>")
                with (patch("elbow_helper.features.agent.scheduled.tools.resolve_channel",
                            new=AsyncMock(return_value=channel)),
                      patch("elbow_helper.features.agent.scheduled.tools.check_post_access")):
                    result = await prepare_save(context, values, registry_factory=dict)
                if kind in ("weekly", "monthly"):
                    self.assertEqual(result["status"], "needs_input")
                    self.assertIn("timezone", result["missing"])
                    self.assertEqual(context.state.proposed_changes, [])
                else:
                    self.assertEqual(result["status"], "confirmation_required")
                    preview = context.state.proposed_changes[0].preview
                    self.assertIn("<t:", "\n".join((*preview.lines, *preview.details)))
                repository.create_standing.assert_not_called()
