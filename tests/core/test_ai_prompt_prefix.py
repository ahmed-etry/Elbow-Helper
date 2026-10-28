"""Provider serialization keeps stable tools and append-only conversation prefixes."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from openai import OpenAIError

from elbow_helper.infrastructure.ai import (
    AgentToolDefinition, AgentToolResult, DeepSeekTextClient, TextGenerationError,
)


def response(content='Answer', calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message={
        'role': 'assistant', 'content': content, 'tool_calls': list(calls),
        'reasoning_content': 'Synthetic reasoning',
    }, finish_reason='stop')], usage=None)


class ProviderPrefixTests(unittest.IsolatedAsyncioTestCase):
    def model(self, provider):
        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(side_effect=provider))), close=AsyncMock())
        transport.with_options = lambda **_: transport
        model = DeepSeekTextClient(None)
        model._client = transport
        self.addAsyncCleanup(model.close)
        return model

    async def test_tool_results_final_answer_and_repair_extend_every_earlier_request(self):
        requests = []
        call = {'id': 'read-1', 'type': 'function',
                'function': {'name': 'alpha', 'arguments': '{}'}}

        async def provider(**options):
            requests.append(deepcopy(options))
            return response('', [call]) if len(requests) == 1 else response()

        model = self.model(provider)
        tools = tuple(AgentToolDefinition(name, name, {'type': 'object'}) for name in ('zulu', 'alpha'))
        session = model.create_agent_session(system_prompt='Fixed rules', prompt='Request', tools=tools)
        await session.advance()
        await session.advance((AgentToolResult('read-1', '{"value":1}'),), allow_tools=False,
                              continuation_instruction='Write the answer.')
        await session.advance(allow_tools=False, continuation_instruction='Repair the answer.')
        for previous, current in zip(requests, requests[1:]):
            self.assertEqual(previous['messages'], current['messages'][:len(previous['messages'])])
            self.assertEqual(previous['tools'], current['tools'])
        self.assertEqual([tool['function']['name'] for tool in requests[0]['tools']], ['alpha', 'zulu'])
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        self.assertEqual(requests[-1]['messages'][2]['reasoning_content'], 'Synthetic reasoning')

    async def test_replacing_tools_is_independent_of_registration_order_and_accepts_empty_tools(self):
        requests = []

        async def provider(**options):
            requests.append(deepcopy(options))
            return response()

        model = self.model(provider)
        tools = tuple(AgentToolDefinition(name, name, {'type': 'object'}) for name in ('zulu', 'alpha'))
        session = model.create_agent_session(system_prompt='Fixed rules', prompt='Request', tools=tools)
        await session.advance()
        session.replace_tools(tuple(reversed(tools)))
        await session.advance()
        self.assertEqual(requests[0]['tools'], requests[1]['tools'])
        session.replace_tools(())
        await session.advance(allow_tools=False)
        self.assertNotIn('tools', requests[-1])
        self.assertEqual(requests[1]['messages'], requests[2]['messages'][:len(requests[1]['messages'])])

    async def test_failed_final_round_preserves_its_prefix_for_retry(self):
        for failure in (OpenAIError('Synthetic failure'), response('')):
            with self.subTest(failure=type(failure).__name__):
                requests = []

                async def provider(**options):
                    requests.append(deepcopy(options))
                    if len(requests) == 1:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return response()

                model = self.model(provider)
                session = model.create_agent_session(system_prompt='Fixed rules', prompt='Request', tools=())
                with self.assertRaises(TextGenerationError):
                    await session.advance(allow_tools=False)
                await session.advance(allow_tools=False, continuation_instruction='Retry the answer.')
                self.assertEqual(requests[0]['messages'],
                                 requests[1]['messages'][:len(requests[0]['messages'])])

