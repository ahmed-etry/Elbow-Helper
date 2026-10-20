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
    + "; ".join(f"{code}={CLANS[code].name}" for code in CLAN_ORDER) + ". "
    "For a member's league resolve their linked accounts, then read summary leagueTier. "
    "For a recruit cohort use read_cohort_leagues with a trial or join report; "
    "account first_seen dates do not establish new recruits."
)
LOOKUP_CAPABILITIES = frozenset({
    "read_clash", "read_message_context", "find_discord_members",
    "read_discord_members", "search_approved_knowledge", "read_conversation_history",
    "read_linked_accounts", "read_active_recruitment_trials", "read_saved_report",
    "read_member_lifecycle", "read_cohort_leagues",
    "remember_task_instruction", "retire_task_instruction",
    "list_supported_attachments", "import_text_attachment",
})

SOCIAL_CAPABILITIES = frozenset({"react_to_request", "find_gif"})
SOCIAL_INSTRUCTION = (
    "This is the short social reply route. For a social image, sticker, GIF or "
    "one-liner, default to a low-effort plan with react_to_request and/or find_gif. "
    "Accompany reactions with one short line or a GIF; a GIF needs no text. "
    "Include any reply text with the plan to avoid another model round. Choose the reaction or GIF for "
    "the actual exchange, not just the media type. If this is a factual request, "
    "correction or serious help, answer appropriately without a social response. "
    "Call load_research_context alone with {} before any lookup or other work; "
    "it loads the full capabilities and context."
)


def social_candidate(question, message):
    """Select a cheap starting route, not the meaning or tone of the reply."""
    from ..files.gif_media import has_image_content

    text = re.sub(r"<@!?\d+>", "", getattr(message, "content", "") or question).strip()
    if len(text) > 300 or simple_lookup(text):
        return False
    if has_image_content(message):
        return True
    return bool(re.fullmatch(
        r"(?:hi|hey|hello|yo|thanks(?: a lot)?|thank you|cheers|lol|lmao|lmfao|"
        r"haha(?:ha)*|nice|gg|well played|my man|you legend|bruh|fair enough|"
        r"roast me|good bot|bad bot)[.! ,\U0001f300-\U0001faff]*", text, re.IGNORECASE,
    ))


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
