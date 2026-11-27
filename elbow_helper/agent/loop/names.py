"""Names the new loop's model reads in place of the bot's machine values: one table per kind.

Applied only at the model boundary (results.shape, the lookup list, the SQL catalogue text and
the community map). Lookups, checks and SQL rows keep the machine values.
"""
from __future__ import annotations

import re

NO_ROLE_SET = "a role not set up on this server"
FEATURE_ACCESS_ONLY_LABEL = "only through its own lookup"

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


def access_labels(guild, role_sets):
    """Each access level as the server's own roles that grant it, e.g. 'Co-Leader or Cross-Leader'.

    Group names stay out of what the model reads; a level with no role on the server reads as NO_ROLE_SET.
    """
    roles = sorted(guild.roles, key=lambda role: -role.position) if guild is not None else []
    labels = {"feature_access_only": FEATURE_ACCESS_ONLY_LABEL}
    for level, role_ids in role_sets.items():
        names = [role.name for role in roles if role.id in role_ids and getattr(role, "name", None)]
        labels[level] = (", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1
                         else names[0] if names else NO_ROLE_SET)
    return labels


def _needs(text, labels):
    match = _NEEDS.fullmatch(text)
    levels = match.group(1).split(", ") if match else ()
    if not levels or not all(level in labels for level in levels):
        return text
    return "This needs " + " and ".join(labels[level] for level in levels) + " access."


def phrase(value):
    if value in PHRASES:
        return PHRASES[value]
    return value.replace("_", " ") if _SNAKE.fullmatch(value) else value


def _permissions(value):
    if isinstance(value, list):
        return [_permissions(item) for item in value]
    return PERMISSIONS.get(value, value) if isinstance(value, str) else value


def _access(value, labels):
    if isinstance(value, list):
        return [_access(item, labels) for item in value]
    return labels.get(value, phrase(value)) if isinstance(value, str) else value


def _value(key, value, labels):
    if key in PERMISSION_KEYS and isinstance(value, list):
        return _permissions(value)
    if key in ACCESS_KEYS and isinstance(value, (str, list)):
        return _access(value, labels)
    if key in WAR_FIELDS and isinstance(value, (str, list)):
        names = WAR_FIELDS[key]
        return [names.get(item, item) if isinstance(item, str) else named(item, labels) for item in value] \
            if isinstance(value, list) else names.get(value, value)
    if not isinstance(value, str):
        return named(value, labels)
    if key in RANK_FIELDS or key in RANK_FIELDS.values():
        return RANKS.get(value, value)
    if key in ("cause", "status") or key.endswith("_status"):
        return phrase(value)
    if key == "error":
        return _needs(value, labels)
    return value


def named(value, labels, *, skip=()):
    """Name the war, permission, rank and access fields; keys in skip stay."""
    if isinstance(value, list):
        return [named(item, labels) for item in value]
    if not isinstance(value, dict):
        return value
    return {(RANK_FIELDS.get(key, key) if key not in skip and
             (item is None or isinstance(item, str) and item in (*RANKS, "")) else key):
            item if key in skip else _value(key, item, labels) for key, item in value.items()}


def codes_line(enum):
    """The argument's codes beside the names results use, for a field whose enum takes them."""
    for table in (RANKS, WAR_TYPES):
        pairs = [f"{code} = {table[code]}" for code in enum if code in table]
        if pairs and (table is not RANKS or "coLeader" in enum):
            return "Codes and the names results show: " + ", ".join(pairs) + "."
    return ""
