"""Small lookup planning with an explicit path to the full research catalogue."""

from __future__ import annotations

import re

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.configuration.clans import CLANS, CLAN_ORDER


EXPAND_CONTEXT = "load_research_context"
EXPAND_DEFINITION = AgentToolDefinition(
    EXPAND_CONTEXT,
    "Load the full capability catalogue, accessible data guide and community knowledge.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
LOOKUP_INSTRUCTION = (
    "This is the small lookup route. Use low effort for a single fact or extraction. "
    "The listed capabilities are a subset, not the limits of the bot. Before a task "
    "needs other capabilities, stored-data schemas, community context, changes, "
    "multi-source analysis or a substantive correction, call load_research_context "
    "alone with {}. It loads the full research route without discarding prior results. "
    "Do not guess unavailable schemas or claim an unlisted capability is unavailable."
    " read_clash accepts family clan codes for clan reads: "
    + "; ".join(f"{code}={CLANS[code].name}" for code in CLAN_ORDER) + "."
)
LOOKUP_CAPABILITIES = frozenset({
    "read_clash", "read_message_context", "find_discord_members",
    "read_discord_members", "search_approved_knowledge", "read_conversation_history",
    "list_supported_attachments", "import_text_attachment",
})


def simple_lookup(question: str) -> bool:
    """Conservatively select short factual requests; uncertain tasks use research.

    This is only an initial cost policy, never an authorization decision. The
    model can expand when context makes an apparently small request complex.
    """
    text = question.strip().lower()
    if len(text) > 300 or re.search(
        r"\b(compare|analy[sz]e|investigate|why|recommend|should|history|trend|"
        r"across|sources|remove|delete|change|add|give|send|post|schedule|"
        r"run|command|prepare|export|spreadsheet)\b", text,
    ):
        return False
    return bool(re.search(
        r"\b(how many|count|extract)\b|"
        r"\b(?:names?|tags?|count)[\s-]+only\b|"
        r"\b(?:only|just)\s+(?:the\s+)?(?:names?|tags?|count)\b|"
        r"\bwhat(?:'s| is)?\b.{0,80}\b(?:league|level|tag|name|town hall)\b",
        text,
    ))
