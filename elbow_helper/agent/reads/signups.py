"""CWL signup and assigned-roster cohorts, chosen by league month."""
from datetime import datetime, timezone

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.rosters.config import CWL_SIGNUP_ROSTER_ID
from elbow_helper.agent.safety.access import accessible_message_channel

# Selectors that pick wars or dates; a roster cycle is one league month.
WAR_SELECTORS = ('after', 'before', 'war_ids', 'latest_completed', 'history_limit')
LEAGUE_MONTHS_ONLY = 'CWL signups and rosters are chosen by league month: use season or season_keys.'


def league_month(cycle):
    """Signups for a league open in the month before it, so a cycle serves the next month's league."""
    opened = datetime.fromtimestamp(cycle.opened_ts, timezone.utc)
    year, month = (opened.year + 1, 1) if opened.month == 12 else (opened.year, opened.month + 1)
    return f'{year:04d}-{month:02d}'


async def league_cycles(context, roster_id):
    """The newest cycle opened for each league month, over every kept cycle of the roster."""
    chosen, before = {}, None
    while True:
        page = await context.roster_queries.cycles(context.guild.id, roster_id, before_id=before, limit=100)
        if page is None:
            return chosen
        for cycle in page.cycles:
            season = league_month(cycle)
            if season not in chosen or (cycle.opened_ts, cycle.id) > (chosen[season].opened_ts, chosen[season].id):
                chosen[season] = cycle
        if page.next_before_id is None:
            return chosen
        before = page.next_before_id


async def cwl_rosters(context, arguments):
    unsupported = sorted(set(arguments).intersection(WAR_SELECTORS))
    if unsupported:
        return {'error': LEAGUE_MONTHS_ONLY, 'cause': 'invalid_arguments', 'unsupported_selectors': unsupported}
    seasons = list(dict.fromkeys([*arguments.get('season_keys', ()),
                                  *([arguments['season']] if arguments.get('season') else [])]))
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
        current = await context.roster_queries.snapshot(context.guild.id, roster.id)
        if current is None:
            missing.append({'roster_id': roster.id, 'cause': 'source_unavailable'})
            continue
        posts = []
        for post in current.posts:
            if await accessible_message_channel(context, post.channel_id) is not None:
                posts.append({'channel_id': post.channel_id, 'message_id': post.message_id})
                context.state.source_channels.add(post.channel_id)
        if not posts:
            missing.append({'roster_id': roster.id, 'cause': 'scope_refused'})
            continue
        snapshots = [current]
        if seasons:
            cycles, snapshots = await league_cycles(context, roster.id), []
            for season in seasons:
                cycle = cycles.get(season)
                if cycle is None:
                    missing.append({'roster_id': roster.id, 'season': season, 'cause': 'missing_history'})
                elif current.cycle is not None and current.cycle.id == cycle.id:
                    snapshots.append(current)
                else:
                    snapshots.append(await context.roster_queries.snapshot(
                        context.guild.id, roster.id, cycle_id=cycle.id))
        for snapshot in snapshots:
            cycle_id = snapshot.cycle.id if snapshot.cycle else None
            rosters.append({'roster_id': roster.id, 'name': roster.name, 'clan_code': roster.clan_code,
                'cycle_id': cycle_id, 'season': league_month(snapshot.cycle) if snapshot.cycle else None,
                'status': roster.status, 'accounts': len(snapshot.members), 'source_posts': posts})
            rows.extend({'roster_id': roster.id, 'cycle_id': cycle_id,
                'member_id': member.discord_user_id,
                'member_name': getattr(context.guild.get_member(member.discord_user_id), 'display_name', None),
                'player_tag': member.player_tag,
                'player_name': member.player_name, 'townHallLevel': member.townhall,
                'clan_code': member.clan_code, 'signed_up_ts': member.signed_up_ts}
                for member in snapshot.members)
    if not rosters and not any(item['cause'] == 'missing_history' for item in missing):
        return {'error': 'No selected CWL roster is accessible here.', 'cause': 'scope_refused',
                'missing_sources': missing}
    population = len(rows)
    rows = [row for row in rows if (
        not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']) and (
        not pool or not arguments.get('clan_codes') or row['clan_code'] in arguments['clan_codes'])]
    rows.sort(key=lambda row: (row['roster_id'], row['cycle_id'] or 0, row['player_tag'], row['member_id']))
    selected_count = len(rows)
    unobserved = sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows})
    return {'rows': rows[:arguments['top_n']] if arguments.get('top_n') else rows,
        'metric': arguments['metric'], 'rosters': rosters,
        'roster_population': population, 'selected_population': selected_count,
        'unobserved_accounts': unobserved,
        'missing_sources': missing, 'selection_complete': not missing,
        'coverage': 'Roster cycles by league month (season) and their published source channels; signup is not final placement.'}
