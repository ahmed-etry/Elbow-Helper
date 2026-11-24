"""Output and conversation tools through the loop: they act through request state, never by posting."""

from contextlib import ExitStack
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID, catalogue_text
from elbow_helper.agent.engine.models import AgentTurnState, ChannelReactions
from elbow_helper.agent.engine.tool_call import AgentUnavailableError
from elbow_helper.agent.loop import service as loop
from tests.agent.engine.helpers import social_step
from tests.agent.request_fixtures import flow_context as _context
from tests.agent.loop_stub import StubModel, StubSession, call, final, loop_service, without_server_notes

MODEL_ONLY = {"react_to_request", "find_gif", "set_discord_reply_target"}
# The reply tool sends memes, so the model is not offered find_gif itself.
LISTED = {"react_to_request", "set_discord_reply_target", "submit_social_reply"}
CONVERSATION = {"remember_task_instruction", "retire_task_instruction", "start_discord_research_job",
                "continue_discord_research_job", "cancel_discord_research_job", "prepare_spreadsheet"}
ATTACHMENTS = {"list_supported_attachments", "import_csv_attachment", "import_xlsx_attachment",
               "import_text_attachment"}


class LoopOutputToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        context = _context()
        context.source_message.channel.send = AsyncMock()
        context.source_message.add_reaction = AsyncMock()
        state = AgentTurnState(channel_reactions=ChannelReactions(emojis=()))
        self.context = replace(context, bot=SimpleNamespace(tree=object()), state=state)

    async def answer(self, *steps):
        session = StubSession(*steps)
        model = StubModel(session)
        answer = await loop_service(model).answer(question="Synthetic question", context=self.context)
        return answer, session, model

    def assert_nothing_posted(self):
        self.context.source_message.channel.send.assert_not_awaited()

    async def test_the_tool_list_has_every_output_tool_and_stays_fixed_through_the_request(self):
        _, session, model = await self.answer(call("react_to_request", {"emoji": "👍"}), final("Ok."))
        names = {tool.name for tool in model.requests[0]["tools"]}
        self.assertLessEqual(LISTED | CONVERSATION | ATTACHMENTS, names)
        self.assertNotIn("find_gif", names)
        self.assertEqual(model.requests[0]["tools"][-1].name, "submit_social_reply")
        self.assertEqual(session.tool_names, [])
        # The loop has no earlier-step results, so the spreadsheet tool must not offer result references.
        sheet = next(tool for tool in model.requests[0]["tools"] if tool.name == "prepare_spreadsheet")
        self.assertNotIn("result references", sheet.description)

    async def test_a_private_pass_keeps_the_spreadsheet_and_drops_what_only_the_room_sees(self):
        tools = loop_service(StubModel()).request_tools(replace(self.context, private_pass=True))
        self.assertFalse(MODEL_ONLY & set(tools))
        self.assertLessEqual({"prepare_spreadsheet", "remember_task_instruction"}, set(tools))

    async def test_a_reaction_alone_is_not_an_answer(self):
        with self.assertRaises(AgentUnavailableError):
            await self.answer(call("react_to_request", {"emoji": "👍"}), final(""))
        self.assert_nothing_posted()

    async def test_a_reaction_is_added_to_the_request_beside_a_line(self):
        answer, _, _ = await self.answer(call("react_to_request", {"emoji": "👍"}), final("Ok."))
        self.assertEqual(answer, "Ok.")
        self.context.source_message.add_reaction.assert_awaited_once_with("👍")
        self.assertEqual(self.context.state.reactions.emojis, ["👍"])
        self.assert_nothing_posted()

    async def test_text_beside_a_reaction_call_is_the_reply(self):
        line = "Synthetic line beside the reaction."
        answer, session, _ = await self.answer(
            call("react_to_request", {"emoji": "👍"}, text=line),
            final("I picked a reaction that fit the exchange."))
        self.assertEqual(answer, line)
        self.context.source_message.add_reaction.assert_awaited_once_with("👍")
        self.assertEqual(session.calls, [()])

    async def test_a_gif_is_set_for_delivery_not_posted(self):
        item = next(iter(ITEM_BY_ID))
        with patch("elbow_helper.agent.discord_actions.gifs.library_gif", return_value="https://example.test/a.gif"):
            answer, _, _ = await self.answer(social_step("Synthetic line.", meme_id=item, gif_requested=True))
        self.assertEqual(answer, "")
        self.assertEqual(self.context.state.gif_url, "https://example.test/a.gif")
        self.assertEqual(self.context.state.gif_fallback_text, "Synthetic line.")
        self.assert_nothing_posted()

    async def test_the_reply_tool_carries_the_old_instruction_and_the_whole_catalogue(self):
        _, _, model = await self.answer(social_step("Synthetic line."))
        tool = model.requests[0]["tools"][-1]
        self.assertIn("A meme that lands is delivered alone", tool.description)
        self.assertIn("Call submit_social_reply for every social reply.", tool.description)
        self.assertTrue(tool.description.endswith("For facts, corrections or serious help, answer without this tool."))
        self.assertNotIn("load_research_context", tool.description)
        self.assertIn(catalogue_text(), tool.parameters["properties"]["meme_shortlist"]["description"])

    async def test_the_model_cannot_call_find_gif_itself(self):
        item = next(iter(ITEM_BY_ID))
        _, session, _ = await self.answer(call("find_gif", {"query": item}), final("Ok."))
        self.assertEqual(json.loads(session.calls[1][0].content), {"error": "unavailable_lookup"})
        self.assertIsNone(self.context.state.gif_url)

    async def test_the_social_reply_is_not_offered_to_a_private_pass(self):
        model = StubModel(StubSession(final("Ok.")))
        await loop_service(model).answer(question="Synthetic question", context=replace(self.context, private_pass=True))
        self.assertNotIn("submit_social_reply", {tool.name for tool in model.requests[0]["tools"]})

    async def test_a_lookup_before_the_reply_and_an_error_after_it_reach_the_model(self):
        answer, session, _ = await self.answer(
            call("read_result_page", {"result_id": "1", "page": 1}, text="I'll check."),
            social_step("", call_id="empty"), social_step("Synthetic line."))
        self.assertEqual(json.loads(session.calls[1][0].content), {"error": "unavailable_result_page"})
        self.assertEqual(json.loads(session.calls[2][0].content), {"error": "Supply the missing social text."})
        self.assertEqual(answer, "Synthetic line.")

    async def test_a_reply_target_is_recorded_for_delivery(self):
        target = SimpleNamespace(author=SimpleNamespace(id=5))
        with patch("elbow_helper.agent.discord_actions.reply_targets._reply_target",
                   AsyncMock(return_value=target)):
            answer, _, _ = await self.answer(call("set_discord_reply_target", {"message_id": 12}, text="Here."))
        self.assertEqual(answer, "Here.")
        self.assertEqual(self.context.state.reply_to, 12)
        self.assertFalse(self.context.state.ping_author)
        self.assert_nothing_posted()

    async def test_a_spreadsheet_is_prepared_as_an_attachment(self):
        self.context.guild.filesize_limit = 10_000_000
        sheets = [{"name": "Members", "columns": ["Name"], "rows": [["A"]]}]
        with patch("elbow_helper.agent.files.spreadsheet_tools.render_workbook_bytes",
                   AsyncMock(return_value=b"xlsx")):
            await self.answer(call("prepare_spreadsheet", {"title": "Synthetic", "sheets": sheets}), final(""))
        attachment, = self.context.state.attachments
        self.assertEqual(attachment.spreadsheet_title, "Synthetic")
        self.assert_nothing_posted()

    async def test_an_instruction_is_kept_in_the_conversation_state_and_is_not_a_lookup(self):
        _, session, _ = await self.answer(
            call("remember_task_instruction", {"label": "Format", "quote": "Synthetic question"}), final("Noted."))
        self.assertIn("instruction", json.loads(session.calls[1][0].content))
        self.assertEqual(len(self.context.state.working.instructions), 1)

    async def test_a_research_job_that_cannot_start_returns_its_error_to_the_model(self):
        _, session, _ = await self.answer(
            call("start_discord_research_job", {"channel_id": 91, "query": "war"}), final("Can't."))
        self.assertIn("error", json.loads(session.calls[1][0].content))
