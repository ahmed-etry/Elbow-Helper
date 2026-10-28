"""Read current guild permissions and scoped channel access without changing roles."""

import discord

from ..access import accessible_message_channel
from ..engine.capability_contract import CapabilityContract
from .core import IDS, family_tool


def allowed(permissions):
    return sorted(name for name, value in permissions if value)


async def discord_access(context, arguments):
    actors = []
    for identifier in arguments.get('role_ids', []):
        role = context.guild.get_role(identifier)
        if role is None:
            return {'error': 'Choose current guild roles.', 'cause': 'needs_input'}
        actors.append(('role', role))
    for name in arguments.get('role_names', []):
        matches = [role for role in context.guild.roles if role.name.casefold() == name.casefold()]
        if len(matches) != 1:
            return {'error': 'Choose one exact guild role name or ID.', 'cause': 'needs_input'}
        actors.append(('role', matches[0]))
    for identifier in arguments.get('member_ids', []):
        member = context.guild.get_member(identifier)
        if member is None:
            return {'error': 'Choose current guild members.', 'cause': 'needs_input'}
        actors.append(('member', member))
    if not actors:
        return {'error': 'Which roles or members should I compare?', 'cause': 'needs_input'}
    actors = list({(kind, actor.id): (kind, actor) for kind, actor in actors}.values())
    channels = []
    for identifier in arguments.get('channel_ids', [context.source_message.channel.id]):
        channel = await accessible_message_channel(context, identifier)
        if channel is None:
            return {'error': 'That channel is outside this destination scope.', 'cause': 'scope_refused'}
        if isinstance(channel, discord.Thread):
            return {'error': 'Choose a guild channel; thread access also depends on thread membership.',
                    'cause': 'invalid_arguments'}
        channels.append(channel)
    rows = []
    for channel in channels:
        context.state.source_channels.add(channel.id)
        for kind, actor in actors:
            permissions = channel.permissions_for(actor)
            base = actor.permissions if kind == 'role' else actor.guild_permissions
            rows.append({'channel_id': channel.id, 'channel_name': channel.name,
                'actor_kind': kind, 'actor_id': actor.id,
                'actor_name': actor.name if kind == 'role' else actor.display_name,
                'guild_permissions': allowed(base), 'channel_permissions': allowed(permissions),
                'view_channel': permissions.view_channel,
                'read_message_history': permissions.read_message_history,
                'role_only': kind == 'role'})
    return {'rows': rows, 'coverage': 'Current guild snapshot and channel overwrites. Role rows include '
            '@everyone plus that role only; member rows combine all held roles and member overwrites. '
            'Role-only access does not establish the access of every holder.'}


TOOLS = (family_tool('read_discord_access',
    'Compare live guild permissions and room access for exact Discord roles or members.',
    {'channel_ids': IDS, 'role_ids': IDS, 'member_ids': IDS,
     'role_names': {'type': 'array', 'minItems': 1, 'maxItems': 25, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}}},
    discord_access, contract=CapabilityContract(
        entity_fields=(('channel_ids', 'discord_channel_set'), ('role_ids', 'discord_role_set'),
                       ('member_ids', 'discord_member_set')),
        source_scope='channel_status', channel_fields=('channel_ids',),
        result_channel_lists=(('rows', 'channel_id'),), result_sources_within_query=True),
    usage_note='Omit channel_ids for this room; compare role_names=["Co-Leader","Cross-Leader"]. Role rows use @everyone + one role; member rows combine held roles.'),)
