"""Resolve an optional named third party before composing social text about them."""

import json

from ..research.member_identity import matches_discord_identity
from ..research.members import find_discord_members


async def social_member_identity(flow, choice):
    """No query, unnamed query and cached self matches perform no lookup.

    A query must quote the current request. A name that could refer to the asker
    is skipped before discovery, even when other members share that name. A
    qualified third-party name can still resolve through the normal matcher.
    """
    query = choice.get("member_query", "").strip()
    context = flow.context
    if (not query or choice.get("self_target", False)
            or query.casefold() not in context.state.request_text.casefold()):
        return None
    asker = context.guild.get_member(context.member.id) or context.member
    if matches_discord_identity(asker, query):
        return None
    if not any(matches_discord_identity(member, query)
               for member in getattr(context.guild, "members", ())):
        return None
    raw = await flow.runner.service.execute_tool(
        name="find_discord_members", handler=find_discord_members,
        arguments={"query": query}, context=context,
    )
    result = json.loads(raw)
    if result.get("status") == "resolved":
        member = result["members"][0]
        if member["member_id"] == context.member.id:
            return None
        return {"status": "resolved", "member": member, "instruction": (
            "Compose the reply about this resolved member. Facts about other members "
            "who share a name, including the asker or owner, are not facts about this one. "
            "Do not transfer their history, relationships or creator identity to this member. "
            "When addressing this member, use their Discord mention, not just a display name. "
            "Otherwise keep this identity internally; public replies and clarifications never show raw IDs."
        )}
    return result
