"""Register a synthetic requester with a real hierarchy in action fixtures."""

from dataclasses import is_dataclass, replace
from types import SimpleNamespace


def register_requester(context, *, position=5):
    member = context.member
    if not hasattr(member, "id"):
        member = SimpleNamespace(id=8)
    if not hasattr(member, "top_role"):
        member.top_role = SimpleNamespace(position=position)
    if not hasattr(member, "guild_permissions"):
        member.guild_permissions = SimpleNamespace(manage_roles=True, manage_nicknames=True)
    if is_dataclass(context):
        context = replace(context, member=member)
    else:
        context.member = member
    get_member = getattr(context.guild, "get_member", lambda _: None)
    context.guild.get_member = lambda identifier: (
        member if identifier == member.id else get_member(identifier)
    )
    return context
