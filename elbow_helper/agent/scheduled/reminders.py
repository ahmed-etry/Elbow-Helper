"""Render confirmed channel reminder recipients without interpreting their text."""

from ..actions.contracts import ActionRefused
from ..conversation.wording import ACTION_UNAVAILABLE


def reminder_members(guild, identifiers):
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50
            or any(type(identifier) is not int or identifier < 1 for identifier in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ActionRefused(ACTION_UNAVAILABLE)
    members = [guild.get_member(identifier) for identifier in identifiers]
    if any(member is None for member in members):
        raise ActionRefused(ACTION_UNAVAILABLE)
    return members


def channel_reminder_content(guild, rule):
    text = rule["text"]
    if "member_ids" not in rule:
        return text  # Saved reminders from before recipients were recorded.
    members = reminder_members(guild, rule["member_ids"])
    return " ".join(member.mention for member in members) + "\n" + text
