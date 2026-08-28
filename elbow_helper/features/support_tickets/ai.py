"""AI-assisted support-ticket copy."""

from __future__ import annotations

import logging

from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.infrastructure.ai import TextGenerator

LOGGER = logging.getLogger(__name__)
FALLBACK_WELCOME = (
    "Leadership opened this ticket to discuss something with you. "
    "Someone will follow up shortly."
)


class SupportWelcomeService:
    """Create optional welcome copy without coupling the feature to an SDK."""

    def __init__(self, text_generator: TextGenerator):
        self._text_generator = text_generator

    async def create(self, topic: str, username: str) -> str:
        prompt = (
            "You are a Discord bot for a Clash of Clans server. "
            f"Leadership opened a private ticket for {username}. "
            f"The topic is '{topic}'. "
            "Craft one concise, neutral sentence that starts with "
            "'Leadership opened this ticket to discuss ...'. "
            "Re-phrase the topic only if necessary for natural grammar, but "
            "keep key terms unchanged. Do not congratulate, celebrate, reassure "
            "that the topic is positive, or use exclamation marks. "
            "Do not add extra game-context clauses."
        )
        try:
            response = await self._text_generator.complete(
                model="gpt-4o-mini",
                prompt=prompt,
                max_tokens=80,
                temperature=0.6,
            )
        except TextGenerationError as error:
            LOGGER.info("AI welcome fallback used: %s", error)
            return FALLBACK_WELCOME
        if not response:
            return FALLBACK_WELCOME
        return response.replace("!", ".")
