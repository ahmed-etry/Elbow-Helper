"""Source audiences and retained-evidence disclosure."""

from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
import asyncio
from dataclasses import dataclass
import logging
from typing import Any

import discord

from .access import (
    READ_ACCESS_ROLE_SETS, ACCESS_VIEW_AUDIT_LOG, KNOWN_ACCESS_REQUIREMENTS,
    has_reader_access_requirements,
)
from .models import AgentRequestContext, EvidenceScope
from elbow_helper.configuration import channels as channel_ids
from elbow_helper.configuration.roles import MEMBERS

LOGGER = logging.getLogger(__name__)

# Integer keys remain reserved for fetched thread memberships. All derived
# audiences share their lifetime, including when an access recheck clears it.
_AUDIENCE_CACHE = object()
_PERMISSION_METHODS = {
    discord.TextChannel: discord.TextChannel.permissions_for,
    discord.ForumChannel: discord.ForumChannel.permissions_for,
    discord.Thread: discord.Thread.permissions_for,
}


def _audience_key(cache, kind, destination, guild):
    generation = cache.setdefault(_AUDIENCE_CACHE, object())
    return (generation, kind, guild.id, getattr(destination, "id", None))


def _read_audience_signature(channel: Any) -> tuple[tuple[int, bool | None, bool | None], ...] | None:
    """Describe role/member read overwrites without relying on cached members."""
    if isinstance(channel, discord.Thread):
        return None
    overwrites = getattr(channel, "overwrites", None)
    if not isinstance(overwrites, Mapping):
        return None
    signature = []
    for target, overwrite in overwrites.items():
        target_id = getattr(target, "id", None)
        view = getattr(overwrite, "view_channel", None)
        history = getattr(overwrite, "read_message_history", None)
        if type(target_id) is not int or view not in (True, False, None) or history not in (True, False, None):
            return None
        signature.append((target_id, view, history))
    return tuple(sorted(signature))


def _public_read_source(channel: Any, guild: discord.Guild) -> bool:
    """A source readable by everyone without role/member read denials."""
    if isinstance(channel, discord.Thread) and channel.is_private():
        return False
    source = channel.parent if isinstance(channel, discord.Thread) else channel
    signature = _read_audience_signature(source)
    default_role = getattr(guild, "default_role", None)
    if signature is None or default_role is None:
        return False
    permissions = source.permissions_for(default_role)
    return bool(
        permissions.view_channel and permissions.read_message_history
        and all(view is not False and history is not False for _, view, history in signature)
    )


def _source_audience_contains_destination(
    source: Any, destination: Any, guild: discord.Guild,
) -> bool:
    source_id = getattr(source, "id", None)
    destination_id = getattr(destination, "id", None)
    if type(source_id) is not int or type(destination_id) is not int:
        return False
    if source_id == destination_id:
        return True
    if _public_read_source(source, guild):
        return True
    source_signature = _read_audience_signature(source)
    destination_signature = _read_audience_signature(destination)
    structurally_equal = bool(
        source_signature is not None
        and destination_signature is not None
        and getattr(source, "type", None) is not None
        and getattr(source, "type", None) == getattr(destination, "type", None)
        and getattr(getattr(source, "guild", None), "id", None)
        == getattr(getattr(destination, "guild", None), "id", None)
        and source_signature == destination_signature
    )
    if not structurally_equal:
        return False
    roles = getattr(guild, "roles", None)
    default_role = getattr(guild, "default_role", None)
    if roles is None or default_role is None or default_role not in roles:
        return False
    for role in roles:
        if type(getattr(getattr(role, "tags", None), "bot_id", None)) is int:
            continue
        destination_permissions = destination.permissions_for(role)
        if not destination_permissions.view_channel:
            continue
        source_permissions = source.permissions_for(role)
        if not (source_permissions.view_channel and source_permissions.read_message_history):
            return False
    return True


def _destination_satisfies_role_requirements(
    channel: Any, guild: discord.Guild, requirements: frozenset[str] | set[str],
    *, failure: dict | None = None,
) -> bool:
    def deny(requirement=None, reader_kind="unknown_member"):
        if failure is not None:
            failure.update(failed_requirement=requirement or sorted(requirements)[0],
                           reader_kind=reader_kind)
        return False

    if not requirements:
        return True
    if ACCESS_VIEW_AUDIT_LOG in requirements:
        return deny(ACCESS_VIEW_AUDIT_LOG, "role_group")
    if not requirements <= KNOWN_ACCESS_REQUIREMENTS:
        return deny(sorted(requirements - KNOWN_ACCESS_REQUIREMENTS)[0])
    destination = channel.parent if isinstance(channel, discord.Thread) else channel
    roles = getattr(guild, "roles", None)
    default_role = getattr(guild, "default_role", None)
    signature = _read_audience_signature(destination)
    if roles is None or default_role is None or signature is None:
        return deny()
    # The owner bypasses channel overwrites, even without an administrator role.
    owner_id = getattr(guild, "owner_id", None)
    if type(owner_id) is not int or not has_reader_access_requirements(guild, owner_id, requirements):
        requirement, kind = _member_access_failure(guild, owner_id, requirements)
        return deny(requirement, kind)
    allowed_ids = set.intersection(*(set(READ_ACCESS_ROLE_SETS[level]) for level in requirements))
    known_roles = {getattr(role, "id", None) for role in roles}
    if not known_roles or getattr(default_role, "id", None) not in known_roles:
        return deny()
    if destination.permissions_for(default_role).view_channel:
        return deny(reader_kind="role_group")
    for role in roles:
        if type(getattr(getattr(role, "tags", None), "bot_id", None)) is int:
            continue
        if destination.permissions_for(role).view_channel and role.id not in allowed_ids:
            requirement = next(level for level in sorted(requirements)
                               if role.id not in READ_ACCESS_ROLE_SETS[level])
            return deny(requirement, "role_group")
    for target, overwrite in destination.overwrites.items():
        if overwrite.view_channel is True and target.id not in known_roles:
            # A member-specific grant is safe only when that member can be
            # resolved and satisfies every requirement under the usual checks.
            if not has_reader_access_requirements(guild, target.id, requirements):
                requirement, kind = _member_access_failure(guild, target.id, requirements)
                return deny(requirement, kind)
    return True


def _reader_kind(member):
    if member is None:
        return "unknown_member"
    return "bot" if getattr(member, "bot", False) else "role_group"


def _member_access_failure(guild, member_id, requirements):
    member = guild.get_member(member_id) if type(member_id) is int else None
    requirement = next((level for level in sorted(requirements)
                        if not has_reader_access_requirements(guild, member_id, {level})),
                       "guild_membership")
    return requirement, _reader_kind(member)


def member_clearance(guild, member_id):
    return frozenset(level for level in KNOWN_ACCESS_REQUIREMENTS
                     if has_reader_access_requirements(guild, member_id, {level}))


async def _thread_member_ids(thread, cache):
    if thread.id not in cache:
        try:
            cache[thread.id] = frozenset(member.id for member in await thread.fetch_members())
        except (discord.DiscordException, OSError, TimeoutError):
            cache[thread.id] = None
    return cache[thread.id]


async def _destination_viewers(destination, guild, cache):
    key = _audience_key(cache, "viewers", destination, guild)
    if key not in cache:
        cache[key] = await _compute_destination_viewers(destination, guild, cache)
    return cache[key]


async def _compute_destination_viewers(destination, guild, cache):
    if not getattr(guild, "chunked", False):
        return None
    if isinstance(destination, discord.Thread):
        if destination.is_private():
            identifiers = await _thread_member_ids(destination, cache)
            if identifiers is None:
                return None
            members = tuple(guild.get_member(identifier) for identifier in identifiers)
            if any(member is None for member in members):
                return None
            moderators = []
            for index, member in enumerate(guild.members, 1):
                permissions = destination.permissions_for(member)
                if permissions.view_channel and getattr(permissions, "manage_threads", False):
                    moderators.append(member)
                if index % 128 == 0:
                    await asyncio.sleep(0)
            return (*members, *moderators)
        destination = destination.parent
    if destination is None:
        return None
    viewers = []
    for index, member in enumerate(guild.members, 1):
        if destination.permissions_for(member).view_channel:
            viewers.append(member)
        if index % 128 == 0:
            await asyncio.sleep(0)
    return tuple(viewers)


async def _destination_humans(destination, guild, cache):
    key = _audience_key(cache, "humans", destination, guild)
    if key not in cache:
        viewers = await _destination_viewers(destination, guild, cache)
        cache[key] = (None if viewers is None else tuple(
            member for member in viewers if not getattr(member, "bot", False)
            and member.id != getattr(guild.me, "id", None)
        ))
    return cache[key]


@dataclass
class _ViewerGroups:
    members: dict[int, discord.Member]
    groups: tuple[dict[int, discord.Member], ...]


async def _viewer_groups(destination, guild, humans, cache):
    key = _audience_key(cache, "groups", destination, guild)
    if key not in cache:
        groups = {}
        for index, member in enumerate(humans, 1):
            # Custom member/permission implementations must use the exact scan.
            if type(member) is not discord.Member or member.guild is not guild:
                cache[key] = None
                return None
            # GuildChannel.permissions_for depends on roles, owner identity,
            # timeout and the source's member overwrites (handled separately).
            group = (tuple(role.id for role in member.roles), member.id == guild.owner_id,
                     member.timed_out_until)
            groups.setdefault(group, {})[member.id] = member
            if index % 128 == 0:
                await asyncio.sleep(0)
        cache[key] = _ViewerGroups({member.id: member for member in humans}, tuple(groups.values()))
    return cache[key]


def _standard_permissions(source, guild):
    method = _PERMISSION_METHODS.get(type(source))
    return (method is not None and getattr(source.permissions_for, "__func__", None) is method
            and source.guild is guild and guild.default_role is not None)


async def _source_readable_by_viewers(source, destination, guild, humans, identifiers, cache):
    groups = None
    overwrites = {}
    if _standard_permissions(source, guild):
        parent = source.parent if isinstance(source, discord.Thread) else source
        overwrites = parent.overwrites
        # Member.roles omits uncached roles. Their source overwrites can still
        # affect permissions, so that case must retain the individual scan.
        unknown_role = any(isinstance(target, discord.Object) and target.type is discord.Role
                           for target in overwrites)
        if not unknown_role:
            groups = await _viewer_groups(destination, guild, humans, cache)
    if groups is None:
        for index, member in enumerate(humans, 1):
            permissions = source.permissions_for(member)
            if not (permissions.view_channel and permissions.read_message_history
                    and (identifiers is None or member.id in identifiers
                         or getattr(permissions, "manage_threads", False))):
                return False
            if index % 128 == 0:
                await asyncio.sleep(0)
        return True

    # Only actual viewers with a member overwrite need individual resolution.
    exceptions = {target.id for target in overwrites
                  if not isinstance(target, discord.Role) and target.id in groups.members}
    for index, identifier in enumerate(exceptions, 1):
        permissions = source.permissions_for(groups.members[identifier])
        if not (permissions.view_channel and permissions.read_message_history
                and (identifiers is None or identifier in identifiers or permissions.manage_threads)):
            return False
        if index % 128 == 0:
            await asyncio.sleep(0)
    joined_or_exception = identifiers | exceptions if identifiers is not None else None
    for index, group in enumerate(groups.groups, 1):
        member = next((member for identifier, member in group.items() if identifier not in exceptions), None)
        if member is None:
            continue
        permissions = source.permissions_for(member)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        if (joined_or_exception is not None and not permissions.manage_threads
                and not group.keys() <= joined_or_exception):
            return False
        if index % 128 == 0:
            await asyncio.sleep(0)
    return True


async def destination_clearance(destination, guild, *, thread_members=None):
    """Intersect access groups before loading any destination evidence."""
    if isinstance(destination, discord.DMChannel):
        recipient = destination.recipient
        return member_clearance(guild, recipient.id) if recipient is not None else frozenset()
    if getattr(getattr(destination, "guild", None), "id", None) != guild.id:
        return frozenset()
    humans = await _destination_humans(destination, guild, thread_members if thread_members is not None else {})
    if humans is not None:
        if humans:
            # Audit entries always require requester-only delivery.
            clearance = KNOWN_ACCESS_REQUIREMENTS
            for index, member in enumerate(humans, 1):
                clearance = clearance & member_clearance(guild, member.id)
                if index % 128 == 0:
                    await asyncio.sleep(0)
            return clearance - {ACCESS_VIEW_AUDIT_LOG}
        return frozenset()
    # An incomplete cache must prove the audience from permission structure.
    if isinstance(destination, discord.Thread) and destination.is_private():
        return frozenset()
    return frozenset(level for level in READ_ACCESS_ROLE_SETS
                     if _destination_satisfies_role_requirements(destination, guild, {level}))


def _role_category_levels(channel, guild, level):
    """Category ownership survives extra grants, but never a narrower read deny."""
    overwrites = getattr(channel, "overwrites", None)
    if not isinstance(overwrites, Mapping):
        return None
    default_id = getattr(getattr(guild, "default_role", None), "id", None)
    for target, overwrite in overwrites.items():
        # @everyone is normally denied on role-owned channels. Other read
        # denials prove a restriction that the category class cannot represent.
        if (target.id != default_id and (
                getattr(overwrite, "view_channel", None) is False
                or getattr(overwrite, "read_message_history", None) is False)):
            return None
        if (target.id == default_id
                and getattr(overwrite, "read_message_history", None) is False):
            return None
    return frozenset({level})


def source_access_levels(channel, guild, *, examination_queries=None):
    """Role-classified sources use semantic access; None means per-person."""
    if getattr(channel, "id", None) == channel_ids.EXAMINATION_PANEL_THREAD:
        return frozenset({"promotion"})
    original_id = getattr(channel, "id", None)
    verify_case = getattr(examination_queries, "is_promotion_case", None)
    if callable(verify_case) and getattr(channel, "type", None) in (
        discord.ChannelType.text, discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    ):
        try:
            promotion_case = verify_case(original_id)
        except ValueError:
            promotion_case = False
        if promotion_case:
            parent = channel.parent if isinstance(channel, discord.Thread) else channel
            return _role_category_levels(parent, guild, "promotion")
    if isinstance(channel, discord.Thread):
        if channel.is_private():
            return None
        channel = channel.parent
    if channel is None:
        return None
    category_id = getattr(channel, "category_id", None)
    if category_id == channel_ids.RECRUITMENT_TICKET_CATEGORY:
        return (_role_category_levels(channel, guild, "recruitment")
                if isinstance(getattr(channel, "overwrites", None), Mapping)
                else frozenset({"recruitment"}))
    if category_id == channel_ids.EXAMINATION_TICKET_CATEGORY:
        return (_role_category_levels(channel, guild, "promotion")
                if isinstance(getattr(channel, "overwrites", None), Mapping)
                else frozenset({"promotion"}))
    if category_id == channel_ids.SUPPORT_TICKET_CATEGORY:
        return None
    # These categories own workflows, even when their category overwrites are
    # empty and individual channels add bots, helpers or applicant grants.
    category_level = {
        channel_ids.RECRUITMENT_CATEGORY: "recruitment",
        channel_ids.PROMOTION_CATEGORY: "promotion",
        channel_ids.CWL_CATEGORY: "cwl",
        channel_ids.LEADERSHIP_CATEGORY: "lead_plus_or_core",
    }.get(category_id)
    if category_level is not None:
        if _public_read_source(channel, guild):
            return frozenset()
        levels = _role_category_levels(channel, guild, category_level)
        if levels is None:
            return None
        grants = getattr(channel, "overwrites", None)
        if not isinstance(grants, Mapping):
            return None
        if any(target.id in MEMBERS and getattr(overwrite, "view_channel", None) is True
               for target, overwrite in grants.items()):
            return frozenset()
        return levels
    identifier = getattr(channel, "id", None)
    classified = {
        channel_ids.REC_ROOM: "recruitment", channel_ids.TRIAL_LIST: "recruitment",
        channel_ids.EXAMINATION_ROOM: "promotion",
        channel_ids.EXAMINATION_PANEL_THREAD: "promotion",
        channel_ids.LEAD_NEWS: "lead_plus_or_core", channel_ids.OVERSEEING_TERRACE: "lead_plus_or_core",
        channel_ids.HIBERNATION_LOG: "lead_plus_or_core",
        **dict.fromkeys(channel_ids.CLAN_LEADERSHIP_CHANNELS.values(), "lead_plus_or_core"),
    }
    if original_id in classified or identifier in classified:
        level = classified.get(original_id, classified.get(identifier))
        return (_role_category_levels(channel, guild, level)
                if isinstance(getattr(channel, "overwrites", None), Mapping)
                else frozenset({level}))
    member_channels = {*channel_ids.CLAN_CHAT_CHANNELS.values(), *channel_ids.CLAN_WAR_CHANNELS.values(),
                       *channel_ids.CLAN_CWL_INFO_CHANNELS.values()}
    if original_id in member_channels or identifier in member_channels or _public_read_source(channel, guild):
        return frozenset()
    overwrites = getattr(channel, "overwrites", None)
    roles = {role.id: role for role in getattr(guild, "roles", ())}
    if not isinstance(overwrites, Mapping):
        return None
    grants = {target.id for target, overwrite in overwrites.items()
              if getattr(overwrite, "view_channel", None) is True}
    if not grants or grants - roles.keys():
        return None
    # Member channels remain member-level even when clan roles restrict joining.
    if grants & MEMBERS:
        return (frozenset() if _role_category_levels(channel, guild, "") is not None
                else None)
    matching = [level for level, ids in READ_ACCESS_ROLE_SETS.items() if grants <= ids]
    if matching:
        smallest = min(matching, key=lambda level: (len(READ_ACCESS_ROLE_SETS[level]), level))
        return _role_category_levels(channel, guild, smallest)
    return None


async def personal_source_allowed(destination, source, guild, *, thread_members=None):
    """Unclassified sources keep exact per-person readership, before reads."""
    if source.id == getattr(destination, "id", None):
        return True
    cache = thread_members if thread_members is not None else {}
    humans = await _destination_humans(destination, guild, cache)
    if humans is None:
        return _source_audience_contains_destination(source, destination, guild)
    if not humans:
        return False
    identifiers = None
    if isinstance(source, discord.Thread) and source.is_private():
        identifiers = await _thread_member_ids(source, cache)
        if identifiers is None:
            return False
    return await _source_readable_by_viewers(source, destination, guild, humans, identifiers, cache)


async def request_scope(context, *, refresh=False):
    if refresh:
        getattr(context, "disclosure_thread_members", {}).clear()
    destination = getattr(context, "delivery_channel", None) or getattr(
        getattr(context, "source_message", None), "channel", None,
    )
    destination_id = getattr(destination, "id", 0)
    requester = bool(getattr(context, "private_pass", False) or getattr(context, "dm_delivery", False)
                     or isinstance(destination, discord.DMChannel))
    state = getattr(context, "state", None)
    cached = getattr(state, "scope", None)
    if (not refresh and cached is not None and cached.destination_id == destination_id
            and cached.requester == requester):
        return cached
    clearance = await destination_clearance(
        destination, context.guild, thread_members=getattr(context, "disclosure_thread_members", None),
    )
    asker = member_clearance(context.guild, context.member.id)
    scope = EvidenceScope(destination_id, clearance, asker if requester else clearance & asker, requester)
    if state is not None:
        state.scope = scope
    return scope


def exclude_source(context, source, reason, *, relevant=False):
    audit = getattr(getattr(context, "state", None), "scope_audit", None)
    if audit is not None:
        audit.excluded[str(source)] = reason
        if relevant:
            audit.private_candidates.add(str(source))


def log_scope(context):
    state = context.state
    scope = state.scope
    if scope is not None:
        LOGGER.info(
            "Agent evidence scope: request=%s destination=%s clearance=%s scope=%s requester=%s "
            "excluded=%s asker_only_offered=%s",
            getattr(context.source_message, "id", None), scope.destination_id,
            sorted(scope.destination_levels), sorted(scope.levels), scope.requester,
            sorted(state.scope_audit.excluded.items()), state.scope_audit.offered,
        )


def log_scope_override(context, private, reply_id):
    """Count each successful disclosure by the classes excluded from its public pass."""
    LOGGER.info(
        "Agent answer disclosure overridden: request=%s requester=%s channel=%s reply=%s "
        "excluded_classes=%s sources=%s levels=%s",
        context.source_message.id, context.member.id,
        getattr(context.delivery_channel or context.source_message.channel, "id", None), reply_id,
        sorted(Counter(context.state.scope_audit.excluded.values()).items()),
        sorted(private.state.source_channels), sorted(private.state.required_access),
    )


async def can_show(destination, sources, access_levels, guild, *, thread_members=None,
                   request_id=None, log_decision=True, examination_queries=None):
    """Preflight provenance for another destination (previews and saved work)."""
    if isinstance(destination, discord.DMChannel):
        return destination.recipient is not None and await can_show_in_dm(
            guild, destination.recipient.id, sources, access_levels,
            examination_queries=examination_queries,
        )
    clearance = await destination_clearance(destination, guild, thread_members=thread_members)
    if not access_levels <= clearance:
        return False
    for source in sources.values():
        if getattr(getattr(source, "guild", None), "id", None) != guild.id or guild.me is None:
            return False
        permissions = source.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        levels = source_access_levels(source, guild, examination_queries=examination_queries)
        if levels is None:
            if not await personal_source_allowed(destination, source, guild, thread_members=thread_members):
                return False
        elif levels and not levels & clearance:
            return False
    return True


async def can_disclose_provenance(context: AgentRequestContext, source_channels, required_access):
    scope = await request_scope(context, refresh=True)
    if not required_access <= scope.levels:
        return False
    from .access import accessible_message_channel
    for channel_id in source_channels:
        if await accessible_message_channel(context, channel_id) is None:
            return False
    return True


async def can_show_in_dm(guild, member_id, sources, levels, *, examination_queries=None):
    if not has_reader_access_requirements(guild, member_id, set(levels)):
        return False
    member = guild.get_member(member_id)
    for source in sources.values() if isinstance(sources, Mapping) else sources:
        channel = guild.get_channel_or_thread(source) if type(source) is int else source
        if channel is None or guild.me is None:
            return False
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        required = source_access_levels(channel, guild, examination_queries=examination_queries)
        if required is not None:
            if required and not any(has_reader_access_requirements(guild, member_id, {level}) for level in required):
                return False
            continue
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        if (isinstance(channel, discord.Thread) and channel.is_private()
                and not getattr(permissions, "manage_threads", False)):
            try:
                await channel.fetch_member(member_id)
            except discord.HTTPException:
                return False
    return True
