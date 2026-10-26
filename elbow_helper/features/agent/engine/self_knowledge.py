"""Read the complete catalogue for overviews and proposed ability claims."""

from __future__ import annotations

import json
import re

from ..access import has_access_requirements, has_reader_access_requirements
from ..actions.contracts import ActionClass
from ..plan.format import PLAN_TOOL_NAME, REPLY_CAPABILITIES


_ABILITY = re.compile(
    r"\b(?:I|we)\s+(?:can\b|cannot\b|can't\b|could(?:n't| not)\b|am able\b|am unable\b|"
    r"don't (?:do|support)\b|do not (?:do|support)\b|support\b|offer\b)|"
    r"\b(?:I'm|I am)\s+(?:able|unable|limited)\b|"
    r"\b(?:my|our)\s+(?:abilities|capabilities|features|tools|remit|scope)\b|"
    r"\b(?:what|things|everything)\s+I\s+(?:can|do|handle|support)\b|"
    r"\bnot something I (?:can |could )?do\b|"
    r"\b(?:outside|beyond)\s+(?:my|the bot's)\s+(?:scope|remit|capabilities)\b",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"\b(?:can|could|would|will|do|are)\s+you\b|"
    r"\b(?:your|bot(?:'s)?)\s+(?:abilities|capabilities|features|limitations)\b",
    re.IGNORECASE,
)
_OVERVIEW_QUESTION = re.compile(
    r"\bwhat(?: else)?\s+(?:can|can't|cannot|do)\s+you\s+(?:do|offer|support|help(?: me)? with)\b|"
    r"\bwhat\s+you\s+can\s+do\b|\bwhat\s+are\s+you\s+capable\s+of\b|"
    r"\b(?:your|bot(?:'s)?)\s+(?:abilities|capabilities|features|limitations)\b",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(
    r"\bnot something I (?:can |could )?do\b|"
    r"\b(?:outside|beyond)\s+(?:my|the bot's)\s+(?:scope|remit|capabilities)\b|"
    r"\bI\s+(?:don't|do not)\s+(?:do|support)\b|"
    r"\bI\s+(?:cannot|can't|am unable to|'m unable to)\s+"
    r"(?:do|edit|change|manage|send|create|delete|schedule|remove|add|set|move|post)\b",
    re.IGNORECASE,
)

# These are the checks in channel_permissions.resolve_targets, channels.editable
# and roles.role_change_access. Target overwrites and hierarchy still need the
# owning feature's live checks; guild permissions alone never prove eligibility.
_PERMISSIONS = {
    "change_channel_permissions": ("manage_roles", "Manage Roles (Manage Permissions in the target channel)"),
    "create_discord_channel": ("manage_channels", "Manage Channels"),
    "edit_discord_channel": ("manage_channels", "Manage Channels"),
    "delete_discord_channel": ("manage_channels", "Manage Channels"),
    "add_discord_roles": ("manage_roles", "Manage Roles"),
    "remove_discord_roles": ("manage_roles", "Manage Roles"),
    "create_discord_role": ("manage_roles", "Manage Roles"),
    "edit_discord_role": ("manage_roles", "Manage Roles"),
}
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


def capability_question(question):
    """Recognize explicit overviews without intercepting ordinary lookup requests."""
    return bool(_OVERVIEW_QUESTION.search(question.replace("\u2019", "'")))


def ability_claim(step, question, *, has_results=False):
    """Inspect deliverable text, never acknowledgements beside executable work."""
    texts = [step.content] if not step.tool_calls else []
    for call in step.tool_calls:
        if call.name not in (PLAN_TOOL_NAME, "submit_social_reply"):
            continue
        try:
            arguments = json.loads(call.arguments)
        except (TypeError, ValueError):
            continue
        if not isinstance(arguments, dict):
            continue
        if call.name == PLAN_TOOL_NAME:
            steps = arguments.get("steps", [])
            if not isinstance(steps, list) or any(
                not isinstance(item, dict) or item.get("capability") not in REPLY_CAPABILITIES
                for item in steps
            ):
                continue
        texts.append(arguments.get("reply" if call.name == PLAN_TOOL_NAME else "text", ""))
    for text in texts:
        if not isinstance(text, str):
            continue
        normalized = text.replace("\u2019", "'")
        if normalized.strip() and capability_question(question):
            return True
        if _UNSUPPORTED.search(normalized):
            return True
        # An offer based on completed reads, or an inability to confirm their
        # missing data, does not describe the bot's overall supported work.
        if not has_results and _ABILITY.search(normalized):
            return True
        # A bare yes/no can assert an ability without a first-person clause.
        if not has_results and _QUESTION.search(question) and re.match(
            r"\W*(?:yes|no|nope|yep|sure)\b", normalized, re.I,
        ):
            return True
    return False


def capability_index(registry, context, commands=None):
    """One deterministic entry per tool, including hidden and denied tools.

    This is implementation metadata, never source evidence or an authorization
    grant. Do not call has_lookup_access: reading the index must not change the
    destination's evidence scope or record a denied data lookup.
    """
    member = context.guild.get_member(context.member.id)
    held = {role.id for role in getattr(member, "roles", ())}
    commands = commands or {}
    entries = {}
    identifiers = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in registry) + r")\b")
    for name, tool in sorted(registry.items()):
        summary = " ".join(tool.definition.description.split())
        # Definitions occasionally refer to another capability by identifier.
        summary = identifiers.sub(lambda match: match.group().replace("_", " "), summary)
        requirements = []
        change = tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)
        required = tool.contract.required_access if tool.contract else frozenset()
        for level in sorted(required):
            label = _ACCESS_LABELS.get(level, level.replace("_", " ").title())
            check_access = has_access_requirements if change else has_reader_access_requirements
            allowed = check_access(context.guild, context.member.id, {level})
            requirements.append(f"needs {label}" + ("; requester lacks access" if not allowed else ""))
        command = commands.get(name)
        if command is not None and command.visible_to is not None:
            roles = [role.name for role in context.guild.roles
                     if role.id in command.visible_to and getattr(role, "name", None)]
            requirements.append("needs " + (" or ".join(roles) or "the command's required roles")
                                + ("; requester lacks access" if not held & command.visible_to else ""))
        if name in _PERMISSIONS:
            flag, label = _PERMISSIONS[name]
            granted = bool(getattr(getattr(member, "guild_permissions", None), flag, False))
            requirements.append(f"needs {label}; requester guild permission={str(granted).lower()}"
                                + ("; verify target permissions" if "channel" in name else ""))
        if change:
            requirements.append("preview and confirmation")
            if context.private_pass or getattr(context.bot, "tree", None) is None:
                requirements.append("changes unavailable in this request")
        entries[name] = (tool.specialist_group, summary +
                         (" [" + "; ".join(requirements) + "]" if requirements else ""))
    return entries


def capability_context(registry, context, commands=None):
    entries = capability_index(registry, context, commands)
    groups = sorted({group for group, _ in entries.values()})
    lines = []
    for group in groups:
        lines.append(group.replace("_", " ") + ":")
        lines.extend("- " + summary for area, summary in entries.values() if area == group)
    return (
        "Before describing or denying an ability, consult this complete registered capability index. "
        "The visible planning tools are only a subset. Answer using this index; "
        "do not claim a supported ability is absent. Describe supported work in ordinary community "
        "language, without tool names, groups, discovery or internal mechanics. State needed permissions "
        "when the requester lacks them. Guild permissions do not establish access to a particular "
        "channel: target permissions, hierarchy, bot permissions and feature-specific access still apply. "
        "Do not promise authorization when these are unchecked. This read performs no action and "
        "enables no tools. Use existing discovery and checked previews if work is actually requested.\n"
        "<complete_capability_index>\n" + "\n".join(lines) + "\n</complete_capability_index>"
    )
