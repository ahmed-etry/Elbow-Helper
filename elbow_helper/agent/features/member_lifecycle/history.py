"""A member's server stays from the family record, with lifecycle's join sources and leave reasons.

Nothing here reads messages: a member writing "joined" or "left" anywhere can
never become a join or a departure.
"""

from datetime import datetime, timezone
import sqlite3

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.agent.safety.access import accessible_message_channel, require_evidence_access
from elbow_helper.agent.research.history import jump_url


def _iso(at):
    return None if at is None else datetime.fromtimestamp(at / 1_000_000, timezone.utc).isoformat()


async def read_member_history(context, arguments):
    await require_evidence_access(context)
    if await accessible_message_channel(context, OVERSEEING_TERRACE) is None:
        return {"error": "The member lifecycle source is not accessible."}
    member_id = arguments.get("member_id")
    if type(member_id) is not int or member_id <= 0:
        return {"error": "Supply a valid member ID."}
    if arguments.get("cursor"):
        # Every stay comes back in one read; no continuation exists.
        return {"error": "That continuation cursor does not match this member history."}
    if context.member_lifecycle_queries is None:
        return {"error": "Member lifecycle observations are not available."}
    try:
        history = await context.member_lifecycle_queries.history(member_id)
    except (ValueError, OSError, sqlite3.Error):
        return {"error": "Stored member lifecycle observations could not be read."}
    if await accessible_message_channel(context, OVERSEEING_TERRACE) is None:
        return {"error": "The member lifecycle source is not accessible."}
    context.state.source_channels.add(OVERSEEING_TERRACE)
    await require_evidence_access(context)
    entries = []
    for entry in history["entries"]:
        message_id = entry["message_id"]
        entries.append({
            "event": entry["event"], "timestamp": _iso(entry["at"]), "basis": entry["basis"],
            "platform": entry["platform"], "reason": entry["reason"],
            "message_id": message_id, "channel_id": OVERSEEING_TERRACE if message_id else None,
            "source": jump_url(context.guild.id, OVERSEEING_TERRACE, message_id) if message_id else None,
        })
    return {
        "member_id": member_id, "channel_id": OVERSEEING_TERRACE, "entries": entries,
        "search_is_exhaustive": True, "complete": history["complete"], "next_cursor": None,
        "coverage": {"recorded_since": _iso(history["recorded_since"]),
                     "earlier_stays_unavailable": True},
        "limitations": [] if history["complete"] else ["server_membership_incomplete"],
    }
