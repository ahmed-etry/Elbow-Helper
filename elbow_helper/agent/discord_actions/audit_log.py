"""Return audit evidence to the model under a retained, private Discord permission boundary."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import discord

from ..access import ACCESS_VIEW_AUDIT_LOG, require_evidence_access, require_lookup_access
from .management import ID, guard, text, tool
from .safety import DiscordActionRefused


@dataclass(frozen=True)
class AuditChange:
    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class AuditEntry:
    entry_id: int
    action_type: str
    member_id: int | None
    member_name: str
    target_id: int | None
    target_name: str
    timestamp: str
    reason: str | None
    changes: list[AuditChange]
    extra: dict[str, Any]


@dataclass(frozen=True)
class AuditSnapshot:
    entries: list[AuditEntry]
    examined_entries: int
    truncated: bool


def audit_tools():
    return (tool(
        "read_discord_audit_log",
        "Read recent audit entries privately, filtered by action or target.",
        {"action_type": text(100), "member_id": ID, "target_id": ID,
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, (), read_audit,
        read=True, entities=(("member_id", "discord_member"), ("target_id", "discord_target")),
        required_access=frozenset({ACCESS_VIEW_AUDIT_LOG}),
    ),)


def value(item):
    if item is None or type(item) in (str, int, float, bool):
        return item
    if isinstance(item, datetime):
        return item.isoformat()
    if isinstance(item, (discord.Permissions, discord.Colour)):
        return item.value
    if isinstance(item, (list, tuple)):
        return [value(child) for child in item]
    if isinstance(item, dict):
        return {str(key): value(child) for key, child in item.items()}
    if hasattr(item, "id"):
        return {"id": item.id, "name": getattr(item, "name", str(item))}
    return str(item)


def entry_row(entry):
    before, after = dict(entry.before), dict(entry.after)
    extra = getattr(entry, "extra", None)
    details = ({"target": value(extra)} if hasattr(extra, "id") else
               {name: value(child) for name, child in vars(extra).items()
                if not name.startswith("_")} if hasattr(extra, "__dict__") else {})
    return AuditEntry(
        entry.id, entry.action.name, getattr(entry, "user_id", getattr(entry.user, "id", None)),
        getattr(entry.user, "display_name", str(getattr(entry.user, "id", ""))),
        getattr(entry.target, "id", None),
        getattr(entry.target, "name", str(entry.target)) if entry.target is not None else "",
        entry.created_at.isoformat(), entry.reason,
        [AuditChange(field, value(before.get(field)), value(after.get(field)))
         for field in sorted(before.keys() | after.keys())], details,
    )


async def read_audit(context, arguments):
    await require_evidence_access(context)
    guard(context, "view_audit_log")
    require_lookup_access(context, {ACCESS_VIEW_AUDIT_LOG})
    options = {"limit": 100}
    if "member_id" in arguments:
        options["user"] = discord.Object(arguments["member_id"])
    if "action_type" in arguments:
        try:
            options["action"] = discord.AuditLogAction[arguments["action_type"]]
        except KeyError as error:
            raise DiscordActionRefused("The selected audit action is invalid.") from error
    matches, scanned = [], 0
    limit = arguments.get("limit", 25)
    async for entry in context.guild.audit_logs(**options):
        scanned += 1
        if ("target_id" not in arguments
                or getattr(entry.target, "id", None) == arguments["target_id"]):
            matches.append(entry_row(entry))
    guard(context, "view_audit_log")
    require_lookup_access(context, {ACCESS_VIEW_AUDIT_LOG})
    context.state.required_access.add(ACCESS_VIEW_AUDIT_LOG)
    await require_evidence_access(context)
    return asdict(AuditSnapshot(matches[:limit], scanned, scanned == 100 or len(matches) > limit))
