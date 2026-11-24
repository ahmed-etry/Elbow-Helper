"""A stub model for the loop service: scripted steps, no provider, no notes or server map."""

from contextlib import ExitStack
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.loop.service import LoopRun, LoopService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage


def call(name, arguments, call_id="call-1", text=""):
    return AgentStep(text, (AgentToolCall(call_id, name, json.dumps(arguments)),), AgentUsage(10, 2, 4, 6))


def final(text, usage=AgentUsage(20, 5, 8, 12)):
    return AgentStep(text, (), usage)


def cut(text):
    """A round the provider stopped at its output limit."""
    return AgentStep(text, (), AgentUsage(20, 5, 8, 12), output_limit_reached=True)


class StubSession:
    context_window_tokens = None

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []
        self.options = []
        self.tool_names = []

    def replace_tools(self, tools):
        self.tool_names.append(sorted(tool.name for tool in tools))

    async def advance(self, results=(), **options):
        self.calls.append(tuple(results))
        self.options.append(options)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class StubModel:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.requests = []

    def create_agent_session(self, **request):
        self.requests.append(request)
        return self.sessions.pop(0) if self.sessions else None


def stub_stored_data():
    return SimpleNamespace(handler=AsyncMock(return_value={}), describe=lambda context: "")


def loop_service(model):
    return LoopService(model, stub_stored_data())


async def banter_answer(context, *steps, question="Synthetic exchange"):
    """One request through the loop with scripted model steps; the answer, the session and the model."""
    from elbow_helper.agent.loop import service as loop
    session = StubSession(*steps)
    model = StubModel(session)
    with ExitStack() as stack:
        without_server_notes(stack)
        stack.enter_context(patch.object(loop, "build_command_tools", return_value=({}, {})))
        answer = await loop_service(model).answer(question=question, context=context)
    return answer, session, model


def without_server_notes(stack: ExitStack):
    """Skip the notes, map and images: they need a whole server, and these tests are about the loop."""
    stack.enter_context(patch.object(LoopRun, "_system_prompt", AsyncMock(return_value="Synthetic system")))
    stack.enter_context(patch.object(LoopRun, "_first_message",
                                     lambda self, context, question, standing_scope="": question))
    stack.enter_context(patch("elbow_helper.agent.loop.service.request_images", AsyncMock(return_value=[])))
    return stack
