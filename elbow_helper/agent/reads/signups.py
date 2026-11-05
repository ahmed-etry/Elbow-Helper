"""Current structured CWL signup and assigned-roster cohorts."""
from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.rosters.config import CWL_SIGNUP_ROSTER_ID
from ..access import accessible_message_channel


async def cwl_rosters(context, arguments):
    unsupported = sorted(set(arguments).intersection(
        ('season', 'season_keys', 'after', 'before', 'war_ids', 'latest_completed', 'history_limit')))
    if unsupported:
        return {'error': 'Current roster cycles do not support historical selectors.',
                'cause': 'invalid_arguments', 'unsupported_selectors': unsupported}
    if context.roster_queries is None:
        return {'error': 'CWL rosters are not available.', 'cause': 'missing_setup'}
    pool = arguments['metric'] == 'signup_pool'
    candidates = await context.roster_queries.list_for_guild(context.guild.id)
    selected = [roster for roster in candidates if
        (roster.id == CWL_SIGNUP_ROSTER_ID if pool else roster.clan_code in CLANS
         and roster.role_id == CLANS[roster.clan_code].cwl_role_id)
        and (pool or not arguments.get('clan_codes') or roster.clan_code in arguments['clan_codes'])]
    rows, rosters, missing = [], [], []
    for roster in selected:
        snapshot = await context.roster_queries.snapshot(context.guild.id, roster.id)
        if snapshot is None:
            missing.append({'roster_id': roster.id, 'cause': 'source_unavailable'})
            continue
        posts = []
        for post in snapshot.posts:
            if await accessible_message_channel(context, post.channel_id) is not None:
                posts.append({'channel_id': post.channel_id, 'message_id': post.message_id})
                context.state.source_channels.add(post.channel_id)
        if not posts:
            missing.append({'roster_id': roster.id, 'cause': 'scope_refused'})
            continue
        rosters.append({'roster_id': roster.id, 'name': roster.name, 'clan_code': roster.clan_code,
            'cycle_id': snapshot.cycle.id if snapshot.cycle else None,
            'cycle_key': snapshot.cycle.cycle_key if snapshot.cycle else None,
            'status': roster.status, 'accounts': len(snapshot.members), 'source_posts': posts})
        rows.extend({'roster_id': roster.id, 'cycle_id': roster.active_cycle_id,
            'member_id': member.discord_user_id, 'player_tag': member.player_tag,
            'player_name': member.player_name, 'townHallLevel': member.townhall,
            'clan_code': member.clan_code, 'signed_up_ts': member.signed_up_ts}
            for member in snapshot.members)
    if not rosters:
        return {'error': 'No selected CWL roster is accessible here.', 'cause': 'scope_refused',
                'missing_sources': missing}
    population = len(rows)
    rows = [row for row in rows if (
        not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']) and (
        not pool or not arguments.get('clan_codes') or row['clan_code'] in arguments['clan_codes'])]
    rows.sort(key=lambda row: (row['roster_id'], row['player_tag'], row['member_id']))
    selected_count = len(rows)
    unobserved = sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows})
    return {'rows': rows[:arguments['top_n']] if arguments.get('top_n') else rows,
        'metric': arguments['metric'], 'rosters': rosters,
        'roster_population': population, 'selected_population': selected_count,
        'unobserved_accounts': unobserved,
        'missing_sources': missing, 'selection_complete': not missing,
        'coverage': 'Current active roster cycles and their published source channels; signup is not final placement.'}
