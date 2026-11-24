"""Compose a banter reply and deliver its text, reaction or GIF."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import discord

from elbow_helper.agent.engine.social_members import REPLIED_MEMBER_INSTRUCTION, social_member_identity
from elbow_helper.agent.engine.social_reply import salvage_choice, social_plan, social_reply_definition
from elbow_helper.agent.plan.checker import PlanCheck, argument_issue
from elbow_helper.agent.research.member_provenance import member_target_issue, request_reply_author_id
from elbow_helper.agent.safety.access import require_evidence_access
from elbow_helper.infrastructure.ai import AgentToolDefinition

from .wording import SOCIAL_REPLY_INSTRUCTION

LOGGER = logging.getLogger(__name__)
GIF_NAME = "find_gif"
_UNRESOLVED_MEMBER = "Member identity could not be resolved. Ask which member without IDs."


def banter_definition(registry, *, combo_due=False):
    """The old tool and schema; its instruction is the description, so the cached prefix holds it once."""
    definition = social_reply_definition(registry, combo_due=combo_due)
    return AgentToolDefinition(
        definition.name, definition.description + " " + SOCIAL_REPLY_INSTRUCTION, definition.parameters,
    )


class Banter:
    """One request's banter calls. A call ends the request with its line, or hands the model an error or identity."""

    def __init__(self):
        self.calls = 0
        self.reply = None
        self.identity_checked = False
        self.identity_query = None
        self.recipient = None
        self.replied_member = None
        self.replied_name = ""

    async def call(self, execute, context, registry, call, arguments):
        """The tool result for one call. `self.reply` is set once the line is final."""
        attempt, self.calls = self.calls, self.calls + 1
        issue = ""
        plan = None
        record = context.operator_debug
        record.submitted([call])
        try:
            choice = salvage_choice(arguments, registry, combo_due=context.state.reaction_combo_due)
            plan = await asyncio.to_thread(
                social_plan, choice, call.call_id, gif_available=GIF_NAME in registry,
                channel_bot_gifs=context.state.channel_bot_gifs, context=context)
            issue = next((found.error for found in (
                argument_issue(registry, step["capability"], step["arguments"], None, step_id=step["id"])
                for step in plan["steps"]) if found is not None), "")
            if (self.identity_query is not None and choice.get("member_query")
                    and choice["member_query"].strip().casefold() != self.identity_query):
                plan = None
                raise ValueError("Keep the resolved member target for this social reply.")
            if not issue and not self.identity_checked:
                identity = await social_member_identity(context, choice)
                if identity is not None:
                    self.identity_checked = True
                    await require_evidence_access(context)
                    if identity.get("status") == "needs_input":
                        self.reply = identity["missing"][0]
                        return json.dumps({"asked": self.reply}, ensure_ascii=False)
                    if identity.get("status") != "resolved":
                        issue = _UNRESOLVED_MEMBER
                    elif not attempt:
                        self.identity_query = choice["member_query"].strip().casefold()
                        if choice.get("address_member"):
                            identity = self._addressed(context, identity)
                        return json.dumps(identity, ensure_ascii=False)
                    else:
                        issue = "Member identity needs a fresh reply. Ask which member without IDs."
                    plan = None
                    raise ValueError(issue)
                if choice.get("address_member") and choice.get("member_query"):
                    raise ValueError(_UNRESOLVED_MEMBER)
            issue = issue or member_target_issue(context, plan.get("reply", ""))
        except (TypeError, ValueError) as error:
            issue = str(error)
        record.checked(call.call_id, PlanCheck(not issue, issue))
        if issue or plan is None:
            return json.dumps({"error": issue}, ensure_ascii=False)
        if not plan.get("reply"):
            return json.dumps({"error": "Supply the missing social text."})
        context.state.gif_requested = choice["gif_requested"]
        for step in plan["steps"]:
            await execute(context, registry, step["capability"], step["arguments"])
        await require_evidence_access(context)
        state = context.state
        sent_id = state.gif_item_id if state.gif_url else None
        LOGGER.info("Agent GIF choice: request=%s shortlist=%s sent=%s skipped_recent=%s",
                    getattr(context.source_message, "id", None), choice["meme_shortlist"], sent_id,
                    state.gif_skipped_recent)
        text = plan["reply"]
        if self.recipient is not None and f"<@{self.recipient}>" not in text and f"<@!{self.recipient}>" not in text:
            text = f"<@{self.recipient}> {text}"
        if self.replied_member is not None:
            # The reply already pings them; a written mention would ping twice.
            text = re.sub(rf"^\s*<@!?{self.replied_member}>\s*", "", text)
            text = re.sub(rf"<@!?{self.replied_member}>", self.replied_name, text)
        if state.gif_url:
            state.gif_fallback_text = text
            state.gif_recipient_id = self.recipient or self.replied_member
            self.reply = ""
        else:
            # Media selection failed or the meme did not fit: the authored line is the reply.
            self.reply = text
        return json.dumps({"sent": sent_id, "reactions": list(state.reactions.emojis)}, ensure_ascii=False)

    def _addressed(self, context, identity):
        """Note who the reply is for; the replied-to message's author is pinged by the reply itself."""
        recipient = identity["member"]["member_id"]
        if recipient != request_reply_author_id(context):
            self.recipient = recipient
            return identity
        # Their message is here: the reply to it, text or GIF, is the one ping.
        context.state.reply_to = context.source_message.reference.message_id
        context.state.ping_author = True
        self.replied_member = recipient
        self.replied_name = discord.utils.escape_mentions(discord.utils.escape_markdown(
            str(identity["member"].get("display_name") or "")))
        return {**identity, "instruction": REPLIED_MEMBER_INSTRUCTION}
