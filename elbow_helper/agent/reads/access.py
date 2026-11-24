"""Read current guild permissions and scoped channel access without changing roles."""

import discord

from elbow_helper.agent.safety.access import accessible_message_channel, can_access_message_channel
from ..engine.capability_contract import CapabilityContract
from elbow_helper.agent.reads.snapshots import IDS, family_tool


def allowed(permissions):
    return sorted(name for name, value in permissions if value)


async def discord_access(context, arguments):
    actors = []
    for identifier in arguments.get('role_ids', []):
        role = context.guild.get_role(identifier)
        if role is None:
            return {'error': 'Choose current guild roles.', 'cause': 'invalid_arguments', 'invalid_fields': ['role_ids']}
        actors.append(('role', role))
    for name in arguments.get('role_names', []):
        matches = [role for role in context.guild.roles if role.name.casefold() == name.casefold()]
        if len(matches) != 1:
            return {'error': 'Choose one exact guild role name or ID.',
                    'cause': 'needs_input' if matches else 'invalid_arguments', 'invalid_fields': ['role_names']}
        actors.append(('role', matches[0]))
    for identifier in arguments.get('member_ids', []):
        member = context.guild.get_member(identifier)
        if member is None:
            return {'error': 'Choose current guild members.', 'cause': 'invalid_arguments', 'invalid_fields': ['member_ids']}
        actors.append(('member', member))
    if not actors:
        return {'error': 'Which roles or members should I compare?', 'cause': 'invalid_arguments',
                'missing_fields': ['role_ids', 'role_names', 'member_ids']}
    actors = list({(kind, actor.id): (kind, actor) for kind, actor in actors}.values())
    channels, left_out = [], 0
    if arguments.get('all_channels'):
        # Threads are not guild channels here: their access also depends on membership.
        for channel in context.guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            if await can_access_message_channel(context, channel, relevant=False):
                channels.append(channel)
            else:
                left_out += 1
    for identifier in () if arguments.get('all_channels') else arguments.get(
            'channel_ids', [context.source_message.channel.id]):
        channel = await accessible_message_channel(context, identifier)
        if channel is None:
            return {'error': 'That channel is outside this destination scope.', 'cause': 'scope_refused'}
        if isinstance(channel, discord.Thread):
            return {'error': 'Choose a guild channel; thread access also depends on thread membership.',
                    'cause': 'invalid_arguments'}
        channels.append(channel)
    # One row per channel; its lists follow `actors`, and each distinct permission list is said once.
    permission_sets, rows = {}, []
    for channel in channels:
        context.state.source_channels.add(channel.id)
        permissions = [channel.permissions_for(actor) for _, actor in actors]
        rows.append({'channel_id': channel.id, 'channel_name': channel.name,
            'view_channel': [value.view_channel for value in permissions],
            'read_message_history': [value.read_message_history for value in permissions],
            'channel_permissions': [permission_sets.setdefault(tuple(allowed(value)), len(permission_sets))
                                    for value in permissions]})
    result = {'actors': [{'actor_kind': kind, 'actor_id': actor.id,
                          'actor_name': actor.name if kind == 'role' else actor.display_name,
                          'guild_permissions': allowed(actor.permissions if kind == 'role'
                                                       else actor.guild_permissions),
                          'role_only': kind == 'role'} for kind, actor in actors],
              'permission_sets': [list(names) for names in permission_sets], 'rows': rows,
              'coverage': 'Current server permissions and channel overwrites. Row lists follow actors; '
              'channel_permissions index permission_sets; differing_channel_ids differ in view or history '
              'access. Role actors include @everyone plus that role only; '
              'member actors combine all held roles and member overwrites. '
              'Role-only access does not establish the access of every holder.'}
    if len(actors) > 1:
        result['differing_channel_ids'] = [
            row['channel_id'] for row in rows
            if len(set(zip(row['view_channel'], row['read_message_history']))) > 1]
    if arguments.get('all_channels'):
        result['channels_left_out'] = left_out
    return result


TOOLS = (family_tool('read_discord_access',
    'Compare live guild permissions and room access for exact Discord roles or members.',
    {'channel_ids': IDS, 'role_ids': IDS, 'member_ids': IDS,
     'role_names': {'type': 'array', 'minItems': 1, 'maxItems': 25, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 1, 'maxLength': 100}},
     'all_channels': {'type': 'boolean', 'description':
                      'Every channel readable here instead of this room (no threads or categories); the rest are counted.'}},
    discord_access, rules=({'needs': ['role_ids', 'role_names', 'member_ids'],
                            'error': 'Which roles or members should I compare?'},
                           {'only_one': ['all_channels', 'channel_ids'],
                            'error': 'Choose all_channels or channel_ids.'}),
    contract=CapabilityContract(
        entity_fields=(('channel_ids', 'discord_channel_set'), ('role_ids', 'discord_role_set'),
                       ('member_ids', 'discord_member_set')),
        source_scope='channel_status', channel_fields=('channel_ids',),
        result_channel_lists=(('rows', 'channel_id'),), result_sources_within_query=True),
    usage_note='Omit channel_ids for this room; compare role_names=["Co-Leader","Cross-Leader"]. Role actors use @everyone + one role; member actors combine held roles.'),)
