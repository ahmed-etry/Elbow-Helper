"""Publishing a lead update previews its message and attachments."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.leadership_news.actions import (
    leadership_news_tools, prepare_lead_news, prepare_news_dismiss,
)


class LeadershipNewsActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_dismissal_prepares_its_registered_class(self):
        prompt = SimpleNamespace(id=8)
        source = SimpleNamespace(id=1, mention="<#1>", fetch_message=AsyncMock(return_value=prompt))
        workflow = SimpleNamespace(public_news_prompt_source=lambda _: 7)
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow),
                                  guild=SimpleNamespace(me=object()), member=object(),
                                  state=AgentTurnState())
        with (patch("elbow_helper.features.agent.capabilities.leadership_news.actions.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.resolve_channel",
                    new_callable=AsyncMock, return_value=source),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.check_post_access")):
            await prepare_news_dismiss(context, {"prompt_message_id": prompt.id})
        tool = next(tool for tool in leadership_news_tools()
                    if tool.definition.name == "dismiss_lead_news_prompt")
        self.assertIs(context.state.proposed_changes[0].action_class, tool.action_class)

    async def test_publication_waits_for_confirm_and_rechecks_source(self):
        attachment = SimpleNamespace(id=5, filename="photo.png", size=10)
        prompt = SimpleNamespace(id=8)
        message = SimpleNamespace(id=7, content="A public update", attachments=[attachment])
        source = SimpleNamespace(id=1, mention="<#1>", fetch_message=AsyncMock(return_value=message))
        target = SimpleNamespace(id=2, mention="<#2>")
        workflow = SimpleNamespace(
            public_news_preview=lambda item: {
                "content": item.content, "attachments": tuple(item.attachments[:3]),
            },
            find_public_news_prompts=AsyncMock(return_value=(prompt,)),
            publish_public_news=AsyncMock(return_value=SimpleNamespace(jump_url="posted-url")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(me=object()), member=object(), state=AgentTurnState(),
        )
        with (patch("elbow_helper.features.agent.capabilities.leadership_news.actions.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.resolve_channel",
                    new_callable=AsyncMock, side_effect=(source, target)),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.check_view_access"),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.check_post_access")):
            result = await prepare_lead_news(context, {"message_id": 7})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIs(next(tool for tool in leadership_news_tools()
                           if tool.definition.name == "publish_lead_news").action_class,
                      ActionClass.CHANGE)
        self.assertTrue(any("A public update" in line for line in action.preview.details))
        self.assertTrue(any("photo.png" in line for line in action.preview.details))
        self.assertTrue(any("publication prompt in" in line for line in action.preview.details))
        self.assertEqual(action.preview.detail_sources, frozenset({source.id}))
        self.assertFalse(any("A public update" in line for line in action.preview.lines))
        self.assertNotIn("A public update", str(result))
        workflow.publish_public_news.assert_not_awaited()
        with (patch("elbow_helper.features.agent.capabilities.leadership_news.actions.check_view_access"),
              patch("elbow_helper.features.agent.capabilities.leadership_news.actions.check_post_access")):
            self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertIn("posted-url", outcome.text)
        workflow.publish_public_news.assert_awaited_once()
        self.assertEqual(workflow.publish_public_news.await_args.kwargs["prompts"], (prompt,))
