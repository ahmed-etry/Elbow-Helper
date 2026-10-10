"""Confirmed direct messages to current server members."""

import asyncio
import io
import logging

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import (
    ACCESS_LEAD,
    LookupAccessDenied,
    has_access_requirements,
    require_evidence_access,
)
from ..actions.contracts import (
    PreparedAction,
    ChangePreview,
    ActionClass,
    ActionRefused,
)
from ..actions.outcomes import ActionOutcome
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool, AgentCapabilityEffect
from ..text import chunk_response
from ..wording import ACTION_DM_LINE, ACTION_DM_LABEL, ACTION_DM_TARGET, DM_SENT_FOR
from .messages import resolve_post_attachment

LOGGER = logging.getLogger(__name__)


class DirectMessageAccessDenied(LookupAccessDenied, ActionRefused):
    """A DM recipient choice requires Lead access."""

    def __init__(self):
        ActionRefused.__init__(self, "DMs to other members need lead access.")
        self.requirements = frozenset({ACCESS_LEAD})


def dm_recipients(context, identifiers):
    if (
        not isinstance(identifiers, list)
        or not 1 <= len(identifiers) <= 50
        or any(type(identifier) is not int for identifier in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise ActionRefused("Choose 1 to 50 distinct current server members.")
    members = [context.guild.get_member(identifier) for identifier in identifiers]
    if any(member is None for member in members):
        raise ActionRefused("Choose 1 to 50 distinct current server members.")
    if any(member.bot for member in members):
        raise ActionRefused("Bots can't receive DMs.")
    if any(member.id != context.member.id for member in members) and not has_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    ):
        raise DirectMessageAccessDenied()
    return members


async def deliver_dms(context, identifiers, text, attachment=None):
    delivery = context.state.direct_messages
    async with delivery.lock:
        return await _deliver_dms(context, identifiers, text, attachment, delivery)


async def _deliver_dms(context, identifiers, text, attachment, delivery):
    members = dm_recipients(context, identifiers)
    results = []
    for index, member in enumerate(members):
        if delivery.sent:
            await asyncio.sleep(1)
        delivery.sent = True
        dm_recipients(context, identifiers)
        asker = context.guild.get_member(context.member.id) or context.member
        footer = (
            "\n\n" + DM_SENT_FOR.format(member=asker.display_name)
            if member.id != context.member.id else ""
        )
        delivered = True
        try:
            for part_index, part in enumerate(chunk_response(text + footer)):
                file = (
                    discord.File(io.BytesIO(attachment.data), filename=attachment.filename)
                    if attachment and part_index == 0 else None
                )
                try:
                    await member.send(
                        part,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=True,
                        ),
                        **({"file": file} if file else {}),
                    )
                finally:
                    if file:
                        file.close()
        except discord.HTTPException as error:
            delivered = False
            LOGGER.info("Agent DM was not delivered: member=%s code=%s", member.id, error.code)
            if error.code not in (50007, 50278):
                LOGGER.warning(
                    "Agent DM was not delivered: member=%s code=%s", member.id, error.code,
                )
        results.append({
            "member_id": member.id,
            "delivered": delivered,
            "label": ACTION_DM_TARGET.format(member=member.display_name),
        })
    return results


async def prepare_direct_messages(context, arguments):
    await require_evidence_access(context)
    members = dm_recipients(context, arguments["member_ids"])
    pending = sum(
        len(action.values.get("member_ids", ()))
        for action in context.state.proposed_changes if action.path == "send_direct_messages"
    )
    if pending + len(members) > 50:
        raise ActionRefused("A confirmation can DM at most 50 members.")
    text = arguments["text"]
    if not isinstance(text, str) or not 1 <= len(text) <= 4000:
        raise ActionRefused("The DM text must be 1 to 4,000 characters.")
    attachment = await resolve_post_attachment(context, arguments)
    if any(member.id != context.member.id for member in members):
        context.state.required_access.add(ACCESS_LEAD)

    async def recheck():
        await require_evidence_access(context)
        dm_recipients(context, arguments["member_ids"])
        return True

    async def run():
        await recheck()
        results = await deliver_dms(context, arguments["member_ids"], text, attachment)
        return ActionOutcome("complete", result={"direct_messages": results})

    context.state.proposed_changes.append(PreparedAction(
        "send_direct_messages",
        dict(arguments),
        ChangePreview(
            (ACTION_DM_LINE.format(members=", ".join(member.mention for member in members)),),
            recheck,
            summary=ACTION_DM_LABEL,
            count=len(members),
            result_label=ACTION_DM_LABEL,
            details=tuple(text.splitlines()),
        ),
        run,
    ))
    return {"status": "confirmation_required"}


def direct_message_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "send_direct_messages",
            "Send confirmed DMs to current members; other recipients need Lead access.",
            {
                "type": "object",
                "properties": {
                    "member_ids": {
                        "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "file_name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "file_message_id": {"type": "integer", "minimum": 1},
                },
                "required": ["member_ids", "text"],
                "additionalProperties": False,
            },
        ),
        prepare_direct_messages,
        AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE,
        contract=CapabilityContract(source_scope="request_context"),
    ),)
