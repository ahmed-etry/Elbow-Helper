"""Discord identities supplied as context, independent of access decisions."""

from typing import Any

from .models import AgentIdentity


def member_identity(member: Any) -> AgentIdentity | None:
    member_id = getattr(member, "id", None)
    name = getattr(member, "display_name", None)
    if type(member_id) is not int or member_id <= 0 or not isinstance(name, str) or not name:
        return None
    return AgentIdentity(name, member_id)


def guild_identity(identity: AgentIdentity, guild: Any) -> AgentIdentity:
    get_member = getattr(guild, "get_member", None)
    member = member_identity(get_member(identity.member_id)) if callable(get_member) else None
    return member if member is not None and member.member_id == identity.member_id else identity


def agent_identity(bot: Any, guild: Any) -> AgentIdentity | None:
    identity = member_identity(getattr(bot, "user", None))
    if identity is None:
        return None
    own_member = member_identity(getattr(guild, "me", None))
    if own_member is not None and own_member.member_id == identity.member_id:
        return own_member
    return guild_identity(identity, guild)
