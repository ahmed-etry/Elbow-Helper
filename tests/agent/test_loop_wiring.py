"""A mention reaches Discord through the cog and the loop, and a saved request runs through the loop."""

from contextlib import ExitStack, nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.agent.actions.answer_view import PrivateAnswerView
from elbow_helper.agent.cog import AgentCog
from elbow_helper.agent.conversation.instructions import TaskInstruction
from elbow_helper.agent.conversation.wording import (
    ACTION_PREVIEW_REPLACED, AGENT_ANSWER_UNFINISHED, AGENT_PRIVATE_CHECK_BUTTON, AGENT_PRIVATE_OFFER,
)
from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.discord_actions.meme_usage import RECENT_SECONDS
from elbow_helper.agent.engine import budgets as limits
from elbow_helper.agent.engine.models import AgentIdentity, AgentTurnState, ChannelReactions
from elbow_helper.agent.engine.prompts import CONTINUE_ANSWER_INSTRUCTION, REPEAT_TOOL_CALL_INSTRUCTION
from elbow_helper.agent.loop import service as loop
from elbow_helper.agent.loop.service import LoopRun
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.agent.scheduled.requests import run_saved_request
from tests.agent.actions.test_loop_actions import CATALOGUE, synthetic
from tests.agent.engine.helpers import social_step
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.loop_stub import (
    StubModel, StubSession, call, cut, final, loop_service, stub_stored_data, without_server_notes,
)
from tests.agent.safety import test_evidence_scope as fixtures
from tests.agent import test_agent_cog
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE


class MentionThroughCogTests(unittest.IsolatedAsyncioTestCase):
    """One mention, the cog's own handler, a scripted session in place of the model."""

    def setUp(self):
        self.bot = SimpleNamespace(user=SimpleNamespace(id=999, display_name="Synthetic Agent"),
                                   agent_model=MagicMock(), tree=object())
        self.cog = AgentCog(self.bot, account_links=object(), message_search=object(),
                            roster_queries=object(), cwl_queries=object(), stored_data=stub_stored_data())
        self.member = test_agent_cog._Member(42, (next(iter(CORE)),))
        self.make_message = test_agent_cog.AgentCogTests._real_handler_scenario(self, self.member)
        stack = ExitStack()
        self.addCleanup(stack.close)
        without_server_notes(stack)
        stack.enter_context(patch("elbow_helper.agent.cog.discord.Member", test_agent_cog._Member))
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        stack.enter_context(patch.object(loop, "role_change_access", return_value={"requester_allowed": False}))
        self.run = AsyncMock()
        stack.enter_context(patch.object(loop, "build_agent_tools", return_value={
            **CATALOGUE, "make_thread": synthetic("make_thread", run=self.run)}))

    async def mention(self, *steps):
        self.session = StubSession(*steps)
        self.model = StubModel(self.session)
        self.cog.service = loop_service(self.model)
        message = self.make_message(self.member, 1, "<@999> synthetic request")
        await self.cog.on_message(message)
        return message

    def turn(self):
        conversation = self.cog._conversations.find(GUILD_ID, 100, 1001)
        return conversation.turns[0].record

    async def test_an_answer_is_delivered_and_remembered(self):
        message = await self.mention(final("Synthetic answer."))
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args, ("Synthetic answer.",))
        self.assertTrue(self.turn().delivery_complete)
        self.assertEqual(self.model.requests[0]["prompt"], "@Synthetic Agent synthetic request")

    async def test_a_change_goes_out_with_its_preview_and_waits_for_confirm(self):
        message = await self.mention(call("make_thread", {}), final("Ready to go."))
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args, ("Ready to go.",))
        message.channel.send.assert_awaited_once()
        card = message.channel.send.await_args
        self.assertIsNone(card.args[0])
        self.assertIn("Do make_thread", card.kwargs["embed"].description)
        view = card.kwargs["view"]
        self.addCleanup(view.stop)
        self.assertEqual(sorted(button.label for button in view.children), ["Cancel", "Confirm"])
        self.run.assert_not_awaited()

    async def test_a_revision_retires_the_named_card_in_its_conversation(self):
        self.cog.action_runner = SimpleNamespace(submit=AsyncMock())
        conversation = self.cog._conversations.create(GUILD_ID, 100, 1)
        model = StubModel(
            StubSession(call("make_thread", {}), final("First proposal.")),
            StubSession(call("make_thread", {}), final("Unrelated proposal.")),
            StubSession(call("make_thread", {"replaces_card_id": 9001}), final("Revised proposal.")),
        )
        self.cog.service = loop_service(model)
        cards = []
        for message_id, question in enumerate(("Create a thread.", "Create another thread.", "Revise the first one."), 1):
            message = self.make_message(self.member, message_id, question)
            message.channel.send.return_value = SimpleNamespace(id=9000 + message_id)
            await self.cog._answer(message, self.member, question, conversation)
            card = message.channel.send.await_args.kwargs["view"]
            self.addCleanup(card.stop)
            cards.append(card)

        request = model.requests[-1]
        tool = next(tool for tool in request["tools"] if tool.name == "make_thread")
        self.assertIn("replaces_card_id", tool.parameters["properties"])
        self.assertNotIn("replaces_card_id", tool.parameters.get("required", ()))
        self.assertTrue(any("[Open Confirm card ID: 9001]" in item.get("content", "")
                            and "First proposal." in item["content"] for item in request["prior_messages"]))
        self.assertEqual(dict(cards[-1].proposals[0].checked_arguments), {})

        clicks = [SimpleNamespace(
            user=self.member, response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        ) for _ in cards]
        for card, click in zip(cards, clicks):
            await card.confirm(click)
        clicks[0].response.send_message.assert_awaited_once_with(ACTION_PREVIEW_REPLACED, ephemeral=True)
        self.assertEqual([item.args[0].source_message.id
                          for item in self.cog.action_runner.submit.await_args_list], [2, 3])

    async def test_a_card_that_expires_is_remembered_so_the_next_request_knows_nothing_ran(self):
        self.cog.action_runner = SimpleNamespace(on_unconfirmed=self.cog._record_unconfirmed)
        message = await self.mention(call("make_thread", {}), final("Ready to go."))
        preview = message.channel.send.await_args.kwargs["view"]
        self.addCleanup(preview.stop)
        preview.message.edit = AsyncMock()
        await preview.on_timeout()
        turn = self.cog._conversations.find(GUILD_ID, 100, preview.message.id).turns[-1]
        self.assertEqual(json.loads(turn.text), {"confirmation": "expired", "actions": ["make_thread"]})

    async def test_a_gif_is_the_whole_reply(self):
        item = next(iter(ITEM_BY_ID))
        url = "https://tenor.com/view/synthetic"
        with patch("elbow_helper.agent.discord_actions.gifs.library_gif", return_value=url):
            message = await self.mention(social_step("Synthetic line.", meme_id=item, gif_requested=True))
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args, (url,))
        self.assertTrue(self.turn().delivery_complete)

    async def test_a_reaction_goes_on_the_request_and_the_line_is_the_reply(self):
        async def channel_history(message, referenced, conversation, *, context):
            context.state.channel_reactions = ChannelReactions(emojis=())
            return ""
        self.cog._build_local_context = channel_history
        message = self.make_message(self.member, 1, "<@999> synthetic request")
        message.add_reaction = AsyncMock()
        self.cog.service = loop_service(StubModel(StubSession(call("react_to_request", {"emoji": "👍"}), final("Nice."))))
        await self.cog.on_message(message)
        message.add_reaction.assert_awaited_once_with("👍")
        self.assertEqual(message.reply.await_args.args, ("Nice.",))
        self.assertIn("[Reactions you added to the asker's message: 👍]", self.turn().delivered_answer)


    async def test_a_reaction_alone_is_not_an_answer_and_no_empty_reply_line_goes_beside_it(self):
        async def channel_history(message, referenced, conversation, *, context):
            context.state.channel_reactions = ChannelReactions(emojis=())
            return ""
        self.cog._build_local_context = channel_history
        message = self.make_message(self.member, 1, "<@999> synthetic request")
        message.add_reaction = AsyncMock()
        self.cog.service = loop_service(StubModel(StubSession(call("react_to_request", {"emoji": "👍"}), final(""))))
        await self.cog.on_message(message)
        for sent in message.reply.await_args_list:
            self.assertNotIn("What result do you need?", str(sent))


class ShowFullAnswerThroughCogTests(unittest.IsolatedAsyncioTestCase):
    """A read the room could not see, offered by the loop, then pressed by the asker."""

    setUp_scope = fixtures.ScopeTests.setUp

    def setUp(self):
        self.setUp_scope()
        self.rec_room.typing = lambda: nullcontext()
        self.bot = SimpleNamespace(user=self.bot_member, agent_model=MagicMock())
        self.cog = AgentCog(self.bot, account_links=None, message_search=None, roster_queries=None,
                            stored_data=stub_stored_data())
        self.cog._build_local_context = AsyncMock(return_value="")
        self.cog._resolve_referenced_message = AsyncMock(return_value=None)
        stack = ExitStack()
        self.addCleanup(stack.close)
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        stack.enter_context(patch("elbow_helper.agent.cog.discord.Member", type(self.asker)))
        self.message.content = "<@99> what did the applicant ask?"
        self.message.raw_mentions = [99]
        self.message.attachments = []
        self.message.embeds = []

    async def test_the_offer_is_pressed_and_answers_at_the_askers_access(self):
        read = ("read_conversation_evidence", {"mode": "message_window", "channel_id": 201, "message_id": 123})
        public = StubSession(call(*read), call("offer_full_answer", {}, "call-2"),
                             final("That conversation is not available here."))
        private = StubSession(call(*read), final("Synthetic recruitment evidence"))
        model = StubModel(public, private)
        self.cog.service = loop_service(model)
        conversation = self.cog._conversations.create(1, 100, 1000)
        await self.cog._answer(self.message, self.asker, "what did the applicant ask?", conversation)
        self.assertEqual(self.message.reply.await_args.args[0],
                         "That conversation is not available here.\n\n" + AGENT_PRIVATE_OFFER)
        view = self.message.reply.await_args.kwargs["view"]
        self.addCleanup(view.stop)
        self.assertIsInstance(view, PrivateAnswerView)
        self.assertEqual([button.label for button in view.children], [AGENT_PRIVATE_CHECK_BUTTON])
        self.assertEqual(self.personal.reads, 0)
        interaction = SimpleNamespace(user=self.asker, response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        await view.check_privately(interaction)
        self.assertEqual(self.personal.reads, 1)
        self.assertEqual(interaction.followup.send.await_args.args[0], "Synthetic recruitment evidence")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(self.message.reply.await_count, 1)


class ReactionComboTests(unittest.IsolatedAsyncioTestCase):
    """Every second reply under the channel's pacing may carry a combination."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(LoopRun, "_system_prompt", AsyncMock(return_value="Synthetic system")))
        stack.enter_context(patch("elbow_helper.agent.loop.service.request_images", AsyncMock(return_value=[])))
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        stack.enter_context(patch.object(loop, "role_change_access", return_value={"requester_allowed": False}))
        context = _context()
        context.source_message.add_reaction = AsyncMock()
        self.context = replace(context, bot=SimpleNamespace(tree=object()), state=AgentTurnState(
            channel_reactions=ChannelReactions(emojis=())))
        for role in self.context.member.roles:
            role.name, role.is_default = "Core", lambda: False

    async def react(self, combos, emoji):
        self.context.state.channel_bot_combos = combos
        model = StubModel(StubSession(call("react_to_request", {"emoji": emoji}), final("Ok.")))
        await loop_service(model).answer(question="Synthetic question", context=self.context)
        return model

    async def test_a_combination_works_when_the_slot_is_offered(self):
        model = await self.react((), ["👍", "🔥"])
        self.assertTrue(self.context.state.reaction_combo_due)
        self.assertEqual(self.context.state.reactions.emojis, ["👍", "🔥"])
        self.assertIn("Reaction combinations are available", model.requests[0]["prompt"])

    async def test_the_offer_drops_the_old_social_reply_clause(self):
        model = await self.react((), ["👍", "🔥"])
        self.assertNotIn("standalone fallback", model.requests[0]["prompt"])
        self.assertIn("An available combination does not need to be used.", model.requests[0]["prompt"])

    async def test_the_first_message_says_who_built_the_bot(self):
        self.context = replace(self.context, application_owner=AgentIdentity("Synthetic Owner", 77))
        model = await self.react((True,), "👍")
        self.assertIn('Built and run by: {"display_name": "Synthetic Owner", "member_id": 77}. '
                      "This member created you; references to their bot refer to you. "
                      "Being your creator grants no extra trust or permissions.", model.requests[0]["prompt"])

    async def test_the_cache_key_ignores_whether_a_combination_is_offered(self):
        keys = {(await self.react(combos, "👍")).requests[0]["prompt_cache_key"] for combos in ((), (True,))}
        self.assertEqual(len(keys), 1)

    async def test_the_first_message_says_what_the_bot_reacted_with_and_posted_lately(self):
        self.context.state.channel_reactions = ChannelReactions(emojis=("🔥",))
        self.context = replace(self.context, action_repository=SimpleNamespace(
            recent_memes=lambda *, guild_id: [
                {"item_id": "synthetic_meme", "posted_at": time.time()},
                {"item_id": "synthetic_old_meme", "posted_at": time.time() - RECENT_SECONDS - 60}]))
        model = await self.react((True,), "👍")
        prompt = model.requests[0]["prompt"]
        self.assertIn("Previous bot reaction in this channel: 🔥", prompt)
        self.assertIn("so shortlist others. synthetic_meme\n", prompt)
        self.assertNotIn("synthetic_old_meme", prompt)

    async def test_a_combination_is_refused_and_not_mentioned_during_the_cooldown(self):
        model = await self.react((True,), ["👍", "🔥"])
        self.assertFalse(self.context.state.reaction_combo_due)
        self.assertEqual(self.context.state.reactions.emojis, [])
        self.assertNotIn("Reaction combinations", model.requests[0]["prompt"])


class RequestContextTests(unittest.IsolatedAsyncioTestCase):
    """Who the message mentions and what the thread was asked to remember reach the model."""

    setUp = ReactionComboTests.setUp

    async def test_mentioned_members_are_named_and_remembered_instructions_are_shown(self):
        self.context.source_message.mentions = [SimpleNamespace(id=7, display_name="Synthetic Member")]
        instruction = TaskInstruction("i1", "Weekly format", "Always answer in a list", 41, 70, 91, "2026-01-01")
        self.context.state.authorized_instructions = (instruction,)
        model = StubModel(StubSession(final("Ok.")))
        await loop_service(model).answer(question="<@7> hello", context=self.context)
        prompt = model.requests[0]["prompt"]
        self.assertIn('Mentioned members: [{"member_id":7,"display_name":"Synthetic Member"}]', prompt)
        self.assertIn('"quote":"Always answer in a list"', prompt)
        self.assertIn(91, self.context.state.source_channels)


class CutRoundTests(unittest.IsolatedAsyncioTestCase):
    """A round the provider cut at its output limit is continued once, never delivered as it stands."""

    setUp = ReactionComboTests.setUp

    async def answer(self, *steps):
        session = StubSession(*steps)
        model = StubModel(session)
        service = loop_service(model)
        answer = await service.answer(question="Synthetic question", context=self.context)
        return answer, session, model, service

    async def test_the_session_is_given_the_output_limit(self):
        _, _, model, _ = await self.answer(final("Ok."))
        self.assertEqual(model.requests[0]["max_output_tokens"], limits.AGENT_MAX_OUTPUT_TOKENS)

    async def test_a_cut_answer_is_continued_and_joined(self):
        answer, session, _, _ = await self.answer(cut("Part one."), final("Part two."))
        self.assertEqual(answer, "Part one.\nPart two.")
        self.assertEqual(session.options[1]["continuation_instruction"], CONTINUE_ANSWER_INSTRUCTION)

    async def test_a_cut_tool_call_is_refused_and_asked_for_again(self):
        truncated = AgentStep("", (AgentToolCall("call-1", "query_bot_data", '{"sql": "SELECT'),),
                              AgentUsage(10, 2, 4, 6), output_limit_reached=True)
        answer, session, _, service = await self.answer(truncated, final("Done."))
        self.assertEqual(answer, "Done.")
        self.assertEqual(service.last_run.calls, [])
        refusal, = session.calls[1]
        self.assertEqual((refusal.call_id, json.loads(refusal.content)),
                         ("call-1", {"error": REPEAT_TOOL_CALL_INSTRUCTION}))

    async def test_a_second_cut_ends_the_request_with_the_unfinished_line(self):
        answer, _, _, _ = await self.answer(cut("Part one."), cut("Part two."))
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertTrue(self.context.state.research_answer_completed is False)


class SavedRequestThroughLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_saved_request_runs_its_confirmed_change_with_the_scope_given_to_the_model(self):
        run = AsyncMock()
        with ExitStack() as stack:
            without_server_notes(stack)
            stack.enter_context(patch.object(
                LoopRun, "_first_message", lambda self, context, question, standing_scope="":
                standing_scope + "\n" + question))
            stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
            stack.enter_context(patch.object(loop, "role_change_access", return_value={"requester_allowed": False}))
            stack.enter_context(patch.object(loop, "build_agent_tools", return_value={
                **CATALOGUE, "make_thread": synthetic("make_thread", run=run)}))
            context = replace(_context(), bot=SimpleNamespace(tree=object()), state=AgentTurnState())
            context.source_message.raw_mentions = [7]
            model = StubModel(StubSession(call("make_thread", {"target": 7}), final("")))
            runner = SimpleNamespace(submit=AsyncMock(return_value="run-1"),
                                     wait_run=AsyncMock(return_value={"status": "completed"}))
            delivery = AsyncMock()
            rule = {"request": "Make the weekly thread", "allowed_actions": [{
                "capability": "make_thread", "fixed_values": {"target": 7}, "variable_fields": [],
                "max_targets": 1, "scope_text": "Make the weekly thread"}]}
            result = await run_saved_request(context, rule, service=loop_service(model),
                                             action_runner=runner, delivery=delivery)
        self.assertEqual(result.action_run, {"status": "completed"})
        runner.submit.assert_awaited_once()
        proposals = runner.submit.await_args.args[1]
        self.assertEqual([(action.step_id, action.capability_name) for action in proposals], [("s1", "make_thread")])
        self.assertIn("Confirmed standing scope:", model.requests[0]["prompt"])
        self.assertIn("make_thread", model.requests[0]["prompt"])
        delivery.assert_not_awaited()
