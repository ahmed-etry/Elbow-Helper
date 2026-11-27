"""Build a community map from complete cached guild facts without I/O."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import unicodedata

import discord

from elbow_helper.configuration import roles as role_config

from .names import PERMISSIONS


POWERS = tuple((key, PERMISSIONS[key]) for key in (
    "administrator", "manage_roles", "manage_channels", "manage_guild", "kick_members", "ban_members",
    "moderate_members", "manage_messages", "mention_everyone",
))
VIEW = discord.Permissions(view_channel=True).value
ADMIN = discord.Permissions(administrator=True).value
MANAGE_ROLES = discord.Permissions(manage_roles=True).value

def shows_whole_layout(roles: frozenset[int]) -> bool:
    """Open owner decision: who is shown every channel the bot can see, not only their own.

    Only channel names and audiences are shown, never contents.
    """
    return bool(roles & (role_config.LEAD | role_config.LEAD_PLUS | role_config.CORE))


@dataclass(frozen=True)
class Role:
    id: int
    name: str
    position: int
    permissions: int
    managed: bool


@dataclass(frozen=True)
class Overwrite:
    id: int
    member: bool
    allow: int
    deny: int


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    type: int
    position: int
    parent_id: int | None
    overwrites: tuple[Overwrite, ...]
    topic: str | None
    # The name with its overwrite members' identities replaced; None when they don't separate cleanly.
    kind: str | None = None

    @property
    def personal(self) -> bool:
        return any(o.member for o in self.overwrites)


@dataclass(frozen=True)
class Member:
    id: int
    roles: frozenset[int]
    bot: bool


@dataclass(frozen=True)
class Server:
    id: int
    owner_id: int | None
    roles: tuple[Role, ...]
    channels: tuple[Channel, ...]
    members: tuple[Member, ...]

    @classmethod
    def from_snapshot(cls, data: dict) -> Server:
        """Consume the supplied REST snapshot, already read off the event loop.

        REST and discord.py expose effective child overwrites even for synced
        channels. Never merge category overwrites into an unsynced child.
        """
        roles = tuple(Role(int(r["id"]), r["name"], r["position"],
                           int(r["permissions"]), r["managed"]) for r in data["roles"])
        guild_id = int(data.get("id") or next(r.id for r in roles if r.position == 0))
        people = {m["id"]: [m.get(key) for key in ("username", "global_name", "nick", "name")]
                  for m in data["members"] if not m["bot"]}
        channels = tuple(Channel(
            int(c["id"]), c["name"], c["type"], c["position"],
            int(c["parent_id"]) if c.get("parent_id") else None,
            tuple(Overwrite(int(o["id"]), o["type"] == 1, int(o["allow"]), int(o["deny"]))
                  for o in c["permission_overwrites"]), c.get("topic"),
            _kind(c["name"], [name for o in c["permission_overwrites"] if o["type"] == 1
                              for name in people.get(o["id"], ())]),
        ) for c in data["channels"])
        members = tuple(Member(int(m["id"]), frozenset(map(int, m["roles"])) | {guild_id},
                               m["bot"]) for m in data["members"])
        owner = data.get("owner_id")
        return cls(guild_id, int(owner) if owner else None, roles, channels, members)

    @classmethod
    def from_guild(cls, guild: discord.Guild) -> Server:
        """Capture a live guild's cache without fetching or doing file work.

        A complete member cache is required for truthful holder counts.
        """
        if guild.member_count is None or guild.member_count != len(guild.members):
            raise ValueError("A complete guild member cache is required")
        return cls(
            guild.id, guild.owner_id,
            tuple(Role(r.id, r.name, r.position, r.permissions.value, r.managed) for r in guild.roles),
            tuple(Channel(
                c.id, c.name, c.type.value, c.position, c.category_id,
                tuple(Overwrite(target.id, not isinstance(target, discord.Role),
                                overwrite.pair()[0].value, overwrite.pair()[1].value)
                      for target, overwrite in c.overwrites.items()), getattr(c, "topic", None),
                _kind(c.name, [name for target in c.overwrites
                               if isinstance(target, discord.Member) and not target.bot
                               for name in (target.name, target.global_name, target.nick)]),
            ) for c in guild.channels),
            tuple(Member(m.id, frozenset(r.id for r in m.roles), m.bot) for m in guild.members),
        )


class Structure:
    """Reusable calculation over one capture, with no per-member text output."""

    def __init__(self, server: Server):
        self.server = server
        # Discord breaks equal positions by lower snowflake first; everyone is last.
        self.roles = tuple(sorted(server.roles, key=lambda r: (
            r.id != server.id, r.position, -r.id), reverse=True))
        self.by_id = {r.id: r for r in self.roles}
        self.channels = {c.id: c for c in server.channels}
        self.members = {m.id: m for m in server.members}
        self.holders = Counter(role for m in server.members for role in m.roles)

    def permissions(self, channel: Channel, roles: frozenset[int], member_id: int | None = None) -> int:
        """Discord's base, everyone, combined roles, then member overwrite order."""
        if member_id is not None and member_id == self.server.owner_id:
            return discord.Permissions.all().value
        base = self.by_id[self.server.id].permissions
        for role_id in roles:
            if role_id in self.by_id:
                base |= self.by_id[role_id].permissions
        if base & ADMIN:
            return discord.Permissions.all().value
        for o in channel.overwrites:
            if not o.member and o.id == self.server.id:
                base = (base & ~o.deny) | o.allow
        allow = deny = 0
        for o in channel.overwrites:
            if not o.member and o.id != self.server.id and o.id in roles:
                allow |= o.allow
                deny |= o.deny
        base = (base & ~deny) | allow
        if member_id is not None:
            for o in channel.overwrites:
                if o.member and o.id == member_id:
                    base = (base & ~o.deny) | o.allow
                    break
        return base

    def can_see(self, channel: Channel, member: Member) -> bool:
        return bool(self.permissions(channel, member.roles, member.id) & VIEW)

    def label(self, role: Role) -> str:
        name = _line(role.name)
        if _divider(role.name):
            name = f"divider #{self.roles.index(role) + 1}"
        # The snapshot contains distinct bot roles with the same name.
        elif sum(r.name == role.name for r in self.roles) > 1:
            name += f" [#{self.roles.index(role) + 1}]"
        return name

    def role_names(self, ids) -> str:
        ids = set(ids)
        names = [self.label(r) for r in self.roles if r.id in ids]
        names.extend(f"missing role {i}" for i in sorted(ids - self.by_id.keys()))
        return ", ".join(names)

    def audience(self, channel: Channel) -> str:
        """Role-only visibility, with combined-role and member exceptions explicit."""
        everyone = bool(self.permissions(channel, frozenset()) & VIEW)
        ordinary = [r for r in self.roles if r.id != self.server.id and not r.permissions & ADMIN]
        viewers = [r.id for r in ordinary if self.permissions(channel, frozenset({r.id})) & VIEW]
        deniers = {o.id for o in channel.overwrites
                   if not o.member and o.id != self.server.id and o.deny & VIEW}
        allowers = {o.id for o in channel.overwrites
                    if not o.member and o.id != self.server.id and o.allow & VIEW}
        if everyone:
            text = "everyone"
        else:
            text = self.role_names(viewers) or "Administrators only"
        # A role deny can matter when VIEW_CHANNEL came from guild permissions;
        # an explicit role allow takes precedence over all role denies.
        everyone_overwrite = next((o for o in channel.overwrites
                                   if not o.member and o.id == self.server.id), None)
        if deniers and (everyone or not everyone_overwrite or not everyone_overwrite.deny & VIEW):
            text += "; role denies: " + self.role_names(deniers)
            if allowers:
                text += "; overridden by: " + self.role_names(allowers)
        return text

    def fixed_text(self) -> str:
        """Shared facts about server roles and which roles can assign them. Holder counts are in holder_text."""
        lines = ["Community roles (highest first; powers are granted by that role)"]
        for role in self.roles:
            permissions = discord.Permissions(role.permissions)
            powers = [label for key, label in POWERS if getattr(permissions, key)]
            markers = ["bot-managed"] if role.managed else []
            if _divider(role.name):
                markers.append("divider")
            lines.append(self.label(role)
                         # Discord's names can hold commas ("Mention @everyone, @here, and All Roles").
                         + ("; " + "; ".join(markers + powers) if markers or powers else ""))
        lines.extend(("\nGiving roles", "Manage Roles also comes from Administrator. A giver can assign "
                      "non-managed roles below their highest held role, excluding @everyone. "
                      "Higher held roles extend the ranges below; dividers retain their place."))
        for index, role in enumerate(self.roles):
            if role.managed or not role.permissions & (MANAGE_ROLES | ADMIN):
                continue
            targets = [r for r in self.roles[index + 1:] if not r.managed and r.id != self.server.id]
            reach = f"starting at {self.label(targets[0])}" if targets else "no assignable roles below it"
            lines.append(f"{self.label(role)}: below {self.label(role)}, {reach} ({len(targets)} roles)")
        return "\n".join(lines)

    def holder_text(self) -> str:
        """Live holder counts, kept out of the cached start because they change as roles are given and removed."""
        lines = ["Community role holders (count = members holding each role, highest role first)"]
        lines.extend(f"{self.label(role)}: {self.holders[role.id]}" for role in self.roles)
        return "\n".join(lines)

    def channel_text(self, asker_id: int, bot_id: int) -> str:
        """Channel names/topics/counts the asker sees, or the bot sees for a whole-layout asker."""
        asker = self.members[asker_id]
        whole = shows_whole_layout(asker.roles)
        viewer = self.members[bot_id] if whole else asker
        visible = {c.id for c in self.server.channels if self.can_see(c, viewer)}
        categories = sorted((c for c in self.server.channels if c.type == 4),
                            key=lambda c: (c.position, c.id))
        parents = [None, *(c.id for c in categories)]
        heading = ("Every channel the bot can see; the asker may not see them all"
                   if whole else "Channels visible to the asker")
        lines = [f"{heading} (role-only audiences; Administrators and the owner bypass "
                 "overwrites; member exceptions are applied last)"]
        for parent in parents:
            children = [c for c in self.server.channels
                        if c.type != 4 and c.parent_id == parent and c.id in visible]
            if not children and parent not in visible:
                continue
            if parent is None:
                lines.append("Uncategorized")
            elif parent in visible:
                category = self.channels[parent]
                exceptions = "; member exceptions" if category.personal else ""
                lines.append(f"{_line(category.name)} [category: {self.audience(category)}{exceptions}]")
            personal = sorted((c for c in children if c.personal), key=lambda c: (c.position, c.id))
            regular = sorted((c for c in children if not c.personal),
                             key=lambda c: (c.type not in (0, 5, 15, 16), c.position, c.id))
            for channel in regular:
                topic = channel.topic or ""
                # Member mentions in otherwise shared channel topics are not member output.
                short_topic = ("; " + _line(topic)) if topic and len(topic) <= 120 and "<@" not in topic else ""
                lines.append(f"  {_line(channel.name)} [{self.audience(channel)}]{short_topic}")
            if personal:
                audiences = Counter(self.audience(c) for c in personal)
                audience = (next(iter(audiences)) if len(audiences) == 1 else
                            " / ".join(f"{count}: {access}" for access, count in sorted(audiences.items())))
                noun = "channel" if len(personal) == 1 else "channels"
                kinds = Counter(_line(c.kind) for c in personal if c.kind)
                named = ": " + ", ".join(f"{kind} ({count})" for kind, count in kinds.items()) if kinds else ""
                lines.append(f"  {len(personal)} member-specific {noun} visible{named} "
                             f"[{audience}; member exceptions]")
        return "\n".join(lines)

    def render(self, asker_id: int, bot_id: int) -> str:
        return (self.fixed_text() + "\n\n"
                + self.channel_text(asker_id, bot_id) + "\n")


def build_structure(guild: discord.Guild | Server, asker_id: int, bot_id: int) -> str:
    """Build a fresh block for one asker. The caller supplies the destination policy.

    The request builder sends shared facts and asker-specific channels separately.
    """
    server = guild if isinstance(guild, Server) else Server.from_guild(guild)
    return Structure(server).render(asker_id, bot_id)


def _kind(name: str, identities) -> str | None:
    """Replace each member's identity in a channel name with "…", as a whole word.

    Discord lowercases channel names and joins words with "-". A name whose
    remainder is empty or that holds none of the identities stays unlabelled.
    """
    kind = name
    for identity in filter(None, identities):
        slug = "-".join(identity.split())
        if any(c.isalnum() for c in slug):
            kind = re.sub(rf"(?<![^\W_]){re.escape(slug)}(?![^\W_])", "…", kind, flags=re.IGNORECASE)
    return kind if kind != name and any(c.isalnum() for c in kind) else None


def _divider(name: str) -> bool:
    return not name or all(c.isspace() or unicodedata.category(c) == "Cf" or c in "\u2800\u3164\uffa0"
                           for c in name)


def _line(text: str) -> str:
    return " ".join(text.splitlines())
