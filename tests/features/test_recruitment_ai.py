from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.recruitment.ai import AIMixin
from elbow_helper.infrastructure.ai import GenerationTier


class _TicketChannel:
    mention = "<#123>"

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self._messages = messages

    def history(self, **_: object):
        async def messages():
            for message in self._messages:
                yield message

        return messages()


class RecruitmentAITests(unittest.IsolatedAsyncioTestCase):
    async def test_second_opinion_uses_the_routine_tier(self) -> None:
        application = SimpleNamespace(
            description=None,
            fields=[SimpleNamespace(name="Why join?", value="To help in wars.")],
        )
        first_message = SimpleNamespace(
            content="Applicant opened a ticket",
            embeds=[SimpleNamespace(), application],
            author=SimpleNamespace(bot=False, display_name="Applicant"),
            attachments=[],
        )
        text_generator = SimpleNamespace(
            complete=AsyncMock(return_value="Recommendation: **Accept**"),
        )
        workflow = AIMixin()
        workflow.text_generator = text_generator

        result = await workflow._build_ticket_second_opinion(
            _TicketChannel([first_message])
        )

        self.assertEqual(
            result,
            ["AI Second Opinion for <#123>\nRecommendation: **Accept**"],
        )
        request = text_generator.complete.await_args.kwargs
        self.assertEqual(request["tier"], GenerationTier.ROUTINE)
        self.assertEqual(request["max_output_tokens"], 650)


if __name__ == "__main__":
    unittest.main()
