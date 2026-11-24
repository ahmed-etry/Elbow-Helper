"""Names the new loop's model reads in place of the bot's machine values: one table per kind.

Applied only at the model boundary (results.shape, the lookup list, the SQL catalogue text and
the community map). Lookups, checks and SQL rows keep the machine values.
"""
from __future__ import annotations

import re

_ACCESS_LABELS = {
    "lead": "Lead", "lead_plus": "Lead Plus", "core": "Core",
    "recruiter": "Recruiter or Lead Plus", "cwl_helper": "CWL helper or Lead Plus",
    "examiner": "Examiner or Lead Plus",
    "recruiter_or_core": "Recruiter or Core", "lead_or_recruiter": "Lead or Recruiter",
    "lead_plus_or_cwl_helper": "Lead Plus or CWL helper",
    "lead_plus_or_core": "Lead Plus or Core",
    "recruitment": "Recruiter, Lead Plus or Core",
    "promotion": "Examiner, Lead Plus or Core", "cwl": "CWL helper, Lead Plus or Core",
}

# Discord's own permission names, as the server settings show them, keyed by discord.py flag.
PERMISSIONS = {
    "add_reactions": "Add Reactions", "administrator": "Administrator", "attach_files": "Attach Files",
    "ban_members": "Ban Members", "change_nickname": "Change Nickname", "connect": "Connect",
    "create_events": "Create Events", "create_expressions": "Create Expressions",
    "create_instant_invite": "Create Invite", "create_private_threads": "Create Private Threads",
    "create_public_threads": "Create Public Threads", "deafen_members": "Deafen Members",
    "embed_links": "Embed Links", "external_emojis": "Use External Emoji",
    "external_stickers": "Use External Stickers", "kick_members": "Kick Members",
    "manage_channels": "Manage Channels", "manage_events": "Manage Events",
    "manage_expressions": "Manage Expressions", "manage_guild": "Manage Server",
    "manage_messages": "Manage Messages", "manage_nicknames": "Manage Nicknames",
    "manage_roles": "Manage Roles", "manage_threads": "Manage Threads", "manage_webhooks": "Manage Webhooks",
    "mention_everyone": "Mention @everyone, @here, and All Roles", "moderate_members": "Timeout Members",
    "move_members": "Move Members", "mute_members": "Mute Members", "priority_speaker": "Priority Speaker",
    "read_message_history": "Read Message History", "read_messages": "View Channels",
    "request_to_speak": "Request to Speak", "send_messages": "Send Messages",
    "send_messages_in_threads": "Send Messages in Threads", "send_polls": "Create Polls",
    "send_tts_messages": "Send Text-to-Speech Messages", "send_voice_messages": "Send Voice Messages",
    "speak": "Speak", "stream": "Video", "use_application_commands": "Use Application Commands",
    "use_embedded_activities": "Use Activities", "use_external_apps": "Use External Apps",
    "use_external_sounds": "Use External Sounds", "use_soundboard": "Use Soundboard",
    "use_voice_activation": "Use Voice Activity", "view_audit_log": "View Audit Log",
    "view_creator_monetization_analytics": "View Server Subscription Insights",
    "view_guild_insights": "View Server Insights",
}
# Clash returns clan ranks as API codes; the game shows these names.
RANKS = {"member": "Member", "admin": "Elder", "coLeader": "Co-Leader", "leader": "Leader"}
RANK_FIELDS = {"role": "in_game_rank"}
WAR_TYPES = {"REG": "regular", "FRIENDLY": "friendly"}
WAR_STATES = {"warEnded": "ended", "inWar": "in war", "notInWar": "not in war",
              "preparation": "preparation"}
# The only result fields that hold a war type or war state; a code anywhere else is not renamed.
WAR_FIELDS = {"war_type": WAR_TYPES, "type": WAR_TYPES, "war_state": WAR_STATES, "state": WAR_STATES}
# Access levels as people here say them; FEATURE_ACCESS_ONLY is the harness's own SQL level.
ACCESS = {**_ACCESS_LABELS, "feature_access_only": "only through its own lookup"}
# Causes and status words; any other snake_case one reads as its words.
PHRASES = {
    "access_denied": "no access", "scope_refused": "not readable here", "needs_input": "needs more input",
    "missing_setup": "not set up", "missing_history": "no stored history", "missing_report": "saved result not found",
    "report_unavailable": "saved result unavailable", "capacity": "too large",
    "sql_schema_error": "unknown table or column", "ownership_unknown": "owner unknown",
    "record_required": "in the family record", "compact_observation_span": "only the span's ends were observed",
    "role_and_destination_permitted": "only what the asker's roles and this room allow",
}

PERMISSION_KEYS = frozenset({"guild_permissions", "permission_sets", "permissions"})
ACCESS_KEYS = frozenset({"required_access", "visibility", "access"})
_SNAKE = re.compile(r"[a-z]+(?:_[a-z]+)+")
# The bot's access refusals all read "This needs <level>[, <level>] access."
_NEEDS = re.compile(r"This needs ([a-z_]+(?:, [a-z_]+)*) access\.")


def _needs(text):
    match = _NEEDS.fullmatch(text)
    levels = match.group(1).split(", ") if match else ()
    if not levels or not all(level in ACCESS for level in levels):
        return text
    return "This needs " + ", ".join(ACCESS[level] for level in levels) + " access."


def phrase(value):
    if value in PHRASES:
        return PHRASES[value]
    return value.replace("_", " ") if _SNAKE.fullmatch(value) else value


def _permissions(value):
    if isinstance(value, list):
        return [_permissions(item) for item in value]
    return PERMISSIONS.get(value, value) if isinstance(value, str) else value


def _access(value):
    if isinstance(value, list):
        return [_access(item) for item in value]
    return ACCESS.get(value, phrase(value)) if isinstance(value, str) else value


def _value(key, value):
    if key in PERMISSION_KEYS and isinstance(value, list):
        return _permissions(value)
    if key in ACCESS_KEYS and isinstance(value, (str, list)):
        return _access(value)
    if key in WAR_FIELDS and isinstance(value, (str, list)):
        names = WAR_FIELDS[key]
        return [names.get(item, item) if isinstance(item, str) else named(item) for item in value] \
            if isinstance(value, list) else names.get(value, value)
    if not isinstance(value, str):
        return named(value)
    if key in RANK_FIELDS or key in RANK_FIELDS.values():
        return RANKS.get(value, value)
    if key in ("cause", "status") or key.endswith("_status"):
        return phrase(value)
    if key == "error":
        return _needs(value)
    return value


def named(value, *, skip=()):
    """Name the war, permission, rank and access fields; keys in skip stay."""
    if isinstance(value, list):
        return [named(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {(RANK_FIELDS.get(key, key) if key not in skip and
             (item is None or isinstance(item, str) and item in (*RANKS, "")) else key):
            item if key in skip else _value(key, item) for key, item in value.items()}


def codes_line(enum):
    """The argument's codes beside the names results use, for a field whose enum takes them."""
    for table in (RANKS, WAR_TYPES):
        pairs = [f"{code} = {table[code]}" for code in enum if code in table]
        if pairs and (table is not RANKS or "coLeader" in enum):
            return "Codes and the names results show: " + ", ".join(pairs) + "."
    return ""
