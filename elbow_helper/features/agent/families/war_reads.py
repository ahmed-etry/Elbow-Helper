"""Typed completed-war attendance, CWL scoring and current-war evidence."""
import asyncio
from .member_context import owner_context
from dataclasses import asdict
from datetime import datetime, timezone
import sqlite3
import json
from collections import defaultdict

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from .core import family_tool, IDS, TAGS, DATE
from ..datasets.clash import read_clash
from .war_metrics import aggregate_performance
from .cwl_summary import cwl_analysis

CLANS_INPUT = {'type': 'array', 'minItems': 1, 'maxItems': 9, 'uniqueItems': True,
               'items': {'type': 'string', 'enum': list(CLANS)}}
SELECTION = {'clan_codes': CLANS_INPUT, 'season': {'type': 'string', 'maxLength': 20},
             'season_keys': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                             'uniqueItems': True, 'items': {'type': 'string', 'maxLength': 20}},
             'latest_completed': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                                  'description': 'Number of individual completed wars across the selected clans, not seasons. Use only for a requested war subset; omit for season ASS/bonus.'},
             'after': DATE, 'before': DATE, 'war_ids': {'type': 'array', 'minItems': 1,
             'maxItems': 56, 'items': {'type': 'string', 'maxLength': 100}}}


def period(arguments):
    values = []
    for field in ('after', 'before'):
        value = arguments.get(field)
        date = datetime.fromisoformat(value) if value else None
        if date is not None and date.utcoffset() is None:
            raise ValueError('Choose timezone-aware dates.')
        values.append(int(date.timestamp()) if date else None)
    after, before = values
    if (after is None) != (before is None) or after is not None and before <= after:
        raise ValueError('Choose a complete date window with its end after its start.')
    return after, before


async def selected_wars(context, arguments, war_type):
    if context.cwl_queries is None:
        return {'error': 'Stored war evidence is not available.', 'cause': 'missing_setup'}
    try:
        if arguments.get('after') and not arguments.get('before'):
            arguments = {**arguments, 'before': context.source_message.created_at.isoformat()}
        after, before = period(arguments)
        selectors = [bool(arguments.get(key)) for key in ('season', 'season_keys', 'war_ids', 'latest_completed', 'history_limit')]
        if sum(selectors) + (after is not None) > 1:
            raise ValueError('Choose one season selection, war selection or date window.')
        if arguments.get('season_keys') and war_type != 'CWL' or war_type == 'both' and arguments.get('season'):
            sources = []
            for season in arguments.get('season_keys', [arguments.get('season')]):
                for kind in ('REG', 'CWL') if war_type == 'both' else (war_type,):
                    source = await selected_wars(context, {**{key: value for key, value in arguments.items()
                        if key != 'season_keys'}, 'season': season}, kind)
                    if 'error' in source:
                        return source
                    sources.append(source)
            return {'rows': [row for source in sources for row in source['rows']],
                    'war_ids': sorted({value for source in sources for value in source['war_ids']}),
                    'source_coverage': [source['coverage'] for source in sources],
                    'coverage': {'query_complete': True, 'historical_collection_complete': False,
                        'exact_ranking': all(source['coverage'].get('exact_ranking', False) for source in sources)}}
        season = arguments.get('season')
        for key in arguments.get('season_keys', []) or ([season] if season else []):
            month = key.removesuffix('-catchup')
            start = datetime.fromisoformat(month + '-01').replace(tzinfo=timezone.utc)
            if start.strftime('%Y-%m') != month:
                raise ValueError('Choose a season in YYYY-MM form, with an optional CWL catchup suffix.')
            if war_type == 'REG':
                if key != month:
                    raise ValueError('Catchup seasons select CWL wars only.')
                end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
                after, before, season = int(start.timestamp()), int(end.timestamp()), None
        if not (after is not None or season or arguments.get('season_keys') or arguments.get('history_limit')
                or arguments.get('war_ids') or arguments.get('latest_completed')):
            return {'error': 'Which season, wars or date window do you want?', 'cause': 'needs_input'}
        return await asyncio.to_thread(context.cwl_queries.attendance,
            clan_codes=arguments.get('clan_codes', list(CLANS)), war_type=war_type,
            after_ts=after, before_ts=before, season=season,
            war_ids=arguments.get('war_ids', ()),
            **{key: arguments[key] for key in ('season_keys', 'history_limit') if arguments.get(key)},
            **({'latest_completed': arguments['latest_completed']} if arguments.get('latest_completed') else {}))
    except ValueError as error:
        return {'error': str(error), 'cause': 'invalid_arguments'}
    except (OSError, sqlite3.Error):
        return {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}


def participation_status(rows):
    if any(type(row.get('attacks_used')) is int and row['attacks_used'] > 0 for row in rows):
        return 'warring'
    if rows and all(row.get('missed') is not None and row.get('attacks_used') == 0 for row in rows):
        return 'joined_without_attacking'
    return 'unknown'


def clan_participation(rows, owners, guild, codes):
    """Distinct linked members and unlinked accounts, separated by observed attacks."""
    summaries = []
    for code in codes:
        selected = [row for row in rows if row.get('clan_code') == code]
        grouped = defaultdict(list)
        for row in selected:
            owner = owners.get(row['player_tag'])
            grouped[('member', owner) if owner else ('account', row['player_tag'])].append(row)
        groups = {status: [] for status in ('warring', 'joined_without_attacking', 'unknown')}
        for (unit, identity), observations in sorted(grouped.items()):
            member = guild.get_member(identity) if unit == 'member' else None
            groups[participation_status(observations)].append({
                'member_id': identity if unit == 'member' else None,
                'member_name': member.display_name if member else None,
                'player_tag': identity if unit == 'account' else None,
                'player_name': observations[0].get('player_name') if unit == 'account' else None,
                'account_tags': sorted({row['player_tag'] for row in observations}), 'unit': unit})
        summaries.append({'clan_code': code, 'wars_observed': len({r['war_id'] for r in selected}),
            **{status: {'member_count': sum(row['unit'] == 'member' for row in identities),
                        'unlinked_account_count': sum(row['unit'] == 'account' for row in identities),
                        'identities': identities} for status, identities in groups.items()}})
    return summaries


async def attendance(context, arguments, *, separate_seasons=False):
    if arguments.get('cohort_ref'):
        from .projections import cohort_selection
        selected = await cohort_selection(context,arguments['cohort_ref'],'account_tags')
        if 'error' in selected:
            return selected
        arguments = {**arguments,**selected}
        if arguments.get('unit') == 'member':
            snapshot = json.loads(context.state.reports[arguments['cohort_ref']].payload)['rows']
            members = sorted({row.get('member_id') or row.get('applicant_member_id') for row in snapshot
                              if row.get('member_id') or row.get('applicant_member_id')})
            if members:
                arguments = {**arguments, 'member_ids': members}
    source = await selected_wars(context, arguments, arguments['war_type'])
    if 'error' in source:
        return source
    tags = sorted({row['player_tag'] for row in source['rows']})
    linked = await asyncio.to_thread(context.account_links.get_links_by_tags, tags) if context.account_links else {}
    owners = {tag: value['discord_user_id'] for tag, value in linked.items() if value}
    rows = [row for row in source['rows'] if (
        not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']) and (
        not arguments.get('member_ids') or owners.get(row['player_tag']) in arguments['member_ids'])]
    if separate_seasons:
        groups = defaultdict(list)
        for row in rows:
            groups[(row['season'], row['clan_code'])].append(row)
        result = [dict(row, season=season, clan_code=code) for (season, code), observations in groups.items()
                  for row in aggregate_attendance(observations, owners, arguments.get('unit', 'account'))]
    else:
        result = aggregate_attendance(rows, owners, arguments.get('unit', 'account'))
    unit = arguments.get('unit', 'account')
    selected_tags = set(arguments.get('account_tags', []))
    if arguments.get('member_ids') and context.account_links:
        member_links = await asyncio.to_thread(context.account_links.get_links_for_members, arguments['member_ids'])
        selected_tags.update(link['player_tag'] for links in member_links.values() for link in links)
    if selected_tags and context.account_links:
        selected_links = await asyncio.to_thread(context.account_links.get_links_by_tags, sorted(selected_tags))
        owners.update({tag: value['discord_user_id'] for tag, value in selected_links.items() if value})
    observed_tags = {row['player_tag'] for row in rows}
    if unit == 'account':
        missing = [(tag, None) for tag in sorted(selected_tags - observed_tags)]
    else:
        observed_members = {owners.get(tag) for tag in observed_tags}
        requested_members = set(arguments.get('member_ids', [])) | {owners[tag] for tag in selected_tags if tag in owners}
        missing = [(None, member) for member in sorted(requested_members - observed_members)]
        missing.extend((tag, None) for tag in sorted(selected_tags - observed_tags) if tag not in owners)
    for tag, member in missing:
        result.append({'member_id': member, 'player_tag': tag, 'player_name': None,
            'account_tags': [tag] if tag else sorted(t for t in selected_tags if owners.get(t) == member),
            'war_ids': [], 'attacks_used': None, 'attacks_expected': None, 'missed': None,
            'miss_rate': None, 'unconfirmed_opportunities': 0, 'unit': 'account' if tag else unit,
            'missing_link': member is None and unit == 'member', 'totals_complete': False,
            'observation_status': 'no_stored_participation'})
    opportunities = defaultdict(list)
    for item in rows:
        owner = owners.get(item['player_tag'])
        key = ('member', owner) if unit == 'member' and owner else ('account', item['player_tag'])
        if separate_seasons:
            key = (item['season'], item['clan_code'], key)
        opportunities[key].append(item)
    for row in result:
        if unit == 'account':
            row['member_id'] = owners.get(row['player_tag'])
        key = ('account', row['player_tag']) if row['player_tag'] else ('member', row['member_id'])
        if separate_seasons:
            key = (row.get('season'), row.get('clan_code'), key)
        row['participation_status'] = participation_status(opportunities[key])
        row['attack_performance'] = {field: value for field, value in
            aggregate_performance(opportunities[key]).items() if field != 'accounts'}
        member = context.guild.get_member(row['member_id']) if row['member_id'] else None
        row.update(owner_context(member, row['member_id']), account_purpose=None)
    measure = arguments.get('sort', 'missed')
    result.sort(key=lambda row: (row.get(measure) is None, -(row.get(measure) or 0),
        -(row['attacks_expected'] or 0), row['member_id'] or 0, row['player_tag'] or ''))
    population = len(result)
    if arguments.get('top_n'):
        result = result[:arguments['top_n']]
    clan_totals = []
    periods = [(season, code) for season in source.get('component_seasons', [arguments.get('season')])
               for code in arguments.get('clan_codes', sorted({r['clan_code'] for r in rows if r.get('clan_code')}))]
    for season, code in periods:
        observations = [row for row in rows if row.get('clan_code') == code
                        and (season is None or row.get('season') == season)]
        selected = [war for war in source.get('war_aliases', []) if war['clan_code'] == code
                    and (season is None or war.get('season') == season)]
        empty = set(source.get('wars_without_rosters', []))
        confirmed = (bool(observations) and all(row.get('missed') is not None for row in observations)
                     and not any(war['war_id'] in empty for war in selected))
        war_count = len(selected) if 'war_aliases' in source else len({row['war_id'] for row in observations})
        clan_totals.append({'clan_code': code, 'season': season, 'wars': war_count,
            'accounts': len({row['player_tag'] for row in observations}),
            'attacks_used': sum(row.get('attacks_used') or 0 for row in observations) if confirmed else None,
            'attacks_expected': sum(row.get('attacks_expected') or 0 for row in observations) if confirmed else None,
            'missed': sum(row['missed'] for row in observations) if confirmed else None,
            'totals_complete': confirmed,
            **({'season_complete': confirmed and war_count >= 7} if separate_seasons and season else {})})
    return {**source, 'rows': result, 'clan_totals': clan_totals,
            'ranked_population': population, 'opportunity_unit': 'account-war',
            'clan_participation': clan_participation(rows, owners, context.guild,
                arguments.get('clan_codes', sorted({r['clan_code'] for r in rows if r.get('clan_code')}))),
            'participation_meanings': {
                'warring': 'Joined a stored war and attacked at least once in the selected period.',
                'joined_without_attacking': 'Joined stored wars but used no attacks; confirmed final participation only.',
                'unknown': 'Attack participation is incomplete or not stored.',
                'counting': 'Distinct members within each clan; unlinked accounts counted separately. A member appearing in several clans is counted in each, so clan counts cannot be summed as unique people.'},
            'ownership_basis': 'current bot links; historical ownership unknown',
            'interpretation': 'Participation describes selected account-war opportunities, not a member reliability verdict. '
                'Account purpose and expected participation remain unknown unless established separately.',
            'ordering': measure + ' descending, expected descending, identity ascending'}


async def current_leagues(context, codes):
    source = await read_clash(context, {'kind': 'clan', 'tags': [CLANS[code].tag for code in codes],
                                        'fields': ['warLeague']})
    items = {row['tag']: row for row in source.get('items', [])}
    return [{'clan_code': code, 'current_league': items.get(CLANS[code].tag, {}).get('data', {}).get('warLeague'),
             'status': items.get(CLANS[code].tag, {}).get('status', source.get('cause', 'unavailable')),
             'observed_at': items.get(CLANS[code].tag, {}).get('observed_at'),
             'season_league': None, 'basis': 'current clan profile; historical participation is separate'} for code in codes]


async def cwl_results(context, arguments):
    if arguments.get('cohort_ref'):
        from .projections import cohort_selection
        selected = await cohort_selection(context,arguments['cohort_ref'],'account_tags')
        if 'error' in selected:
            return selected
        arguments = {**{key: value for key, value in arguments.items() if key != 'cohort_ref'}, **selected}
    if arguments.get('metric') in ('signup_pool', 'assigned_rosters'):
        from .signup_reads import cwl_rosters
        return await cwl_rosters(context, arguments)
    metric = arguments.get('metric', 'performance')
    clan = arguments.get('clan_codes', [])
    if metric in ('ass', 'bonus') and len(clan) > 1:
        rows, summaries, gaps, population = [], [], [], 0
        for code in clan:
            part = await cwl_results(context, {**{key: value for key, value in arguments.items()
                if key != 'top_n'}, 'clan_codes': [code]})
            if 'error' in part:
                gaps.append({'clan_code': code, 'cause': part.get('cause'), 'error': part['error']})
                continue
            rows.extend(dict(row, clan_code=code) for row in part['rows'])
            population += part.get('ranked_population', len(part['rows']))
            summaries.append({'clan_code': code, **{key: value for key, value in part.items()
                if key not in ('rows', 'unobserved_accounts', 'selected_group_metrics',
                               'score_comparisons', 'analysis_scope')}})
        selected_rows = rows[:arguments['top_n']] if arguments.get('top_n') else rows
        for summary in summaries:
            summary['top_rows'] = [row for row in selected_rows
                if row['clan_code'] == summary['clan_code'] and row.get('rank') is not None][:3]
        return {'rows': selected_rows,
                **cwl_analysis(rows, metric, scopes=summaries),
                'metric': metric, 'clan_results': summaries, 'missing_sources': gaps,
                'ranked_population': population, 'status': 'partial' if gaps else 'resolved',
                'unobserved_accounts': sorted(set(arguments.get('account_tags', []))
                    - {row['player_tag'] for row in rows}),
                'rank_scope': 'separate ranking for each selected clan and season',
                'coverage': 'stored completed CWL wars; per-clan scoring settings and gaps remain explicit'}
    league_rows = await current_leagues(context, clan or list(CLANS)) if metric == 'league' else []
    if metric == 'league' and not any(arguments.get(key) for key in
            ('season', 'season_keys', 'after', 'before', 'war_ids', 'latest_completed')):
        return {'rows': league_rows, 'current_clan_leagues': league_rows, 'metric': metric,
                'coverage': 'Current clan profile league labels; participation is not inferred.'}
    if context.cwl_queries is None:
        if metric == 'league':
            return {'rows': [], 'current_clan_leagues': league_rows, 'metric': metric,
                    'missing_sources': [{'source': 'CWL history', 'cause': 'missing_setup'}],
                    'coverage': 'Current profiles only; historical season leagues remain unknown.'}
        return {'error': 'CWL results are not available.', 'cause': 'missing_setup'}
    try:
        if metric == 'attendance':
            if not any(arguments.get(key) for key in ('season', 'season_keys', 'history_limit', 'war_ids',
                                                      'latest_completed', 'after', 'before')):
                # "Last CWL" means the latest completed season, not its last war.
                arguments = {**arguments, 'history_limit': 1}
            result = await attendance(context, {**arguments, 'war_type': 'CWL'}, separate_seasons=True)
            return {**result, 'metric': metric,
                    'period_unit': 'season' if any(arguments.get(key) for key in ('season', 'season_keys', 'history_limit'))
                        else 'war' if arguments.get('latest_completed') or arguments.get('war_ids') else 'date_window',
                    **({'season': arguments['season']} if arguments.get('season') else {}),
                    **({'requested_seasons': arguments['history_limit']} if arguments.get('history_limit') else {})}
        if arguments.get('season_keys'):
            if metric == 'mega_ass':
                return {'error': 'Choose the MEGA ASS history depth rather than separate season scores.', 'cause': 'invalid_arguments'}
            parts = []
            for season in sorted(arguments['season_keys'], reverse=True):
                part = await cwl_results(context, {**{key: value for key, value in arguments.items()
                    if key not in ('season_keys', 'top_n')}, 'season': season})
                if 'error' in part:
                    return part
                parts.append(part)
            rows = [dict(row, season=row.get('season') or season) for season, part in
                    zip(sorted(arguments['season_keys'], reverse=True), parts) for row in part['rows']]
            return {'rows': rows[:arguments['top_n']] if arguments.get('top_n') else rows,
                    'metric': metric, 'component_seasons': sorted(arguments['season_keys'], reverse=True),
                    'ranked_population': sum(part.get('ranked_population', len(part['rows'])) for part in parts),
                    'clan_seasons': [row for part in parts for row in part.get('clan_seasons', [])],
                    'clan_totals': [row for part in parts for row in part.get('clan_totals', [])],
                    'missing_sources': [row for part in parts for row in part.get('missing_sources', [])],
                    'attendance_coverage': [part['attendance_coverage'] for part in parts if 'attendance_coverage' in part],
                    'unobserved_accounts': sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows}),
                    'coverage': [part.get('coverage') for part in parts], 'score_scope': 'separate season results'}
        if metric == 'attendance' or metric == 'performance' and any(arguments.get(key) for key in ('after', 'before', 'war_ids', 'latest_completed')):
            result = await attendance(context, {**arguments, 'war_type': 'CWL'})
            return {**result, 'metric': metric,
                    'period_unit': 'season' if arguments.get('season') else 'war' if arguments.get('latest_completed') or arguments.get('war_ids') else 'date_window',
                    **({'season': arguments['season']} if arguments.get('season') else {})}
        if metric in ('ass', 'bonus'):
            if len(clan) != 1:
                return {'error': 'Which clan should I score?', 'cause': 'needs_input'}
            if not any(arguments.get(key) for key in (
                    'season', 'after', 'before', 'war_ids', 'latest_completed')):
                season = await asyncio.to_thread(context.cwl_queries.latest_season, clan_code=clan[0])
                if season is None:
                    return {'rows': [], 'metric': metric, 'seasons': [],
                            'coverage': 'No stored completed CWL wars for the selected clan.'}
                arguments = {**arguments, 'season': season}
            selected = await selected_wars(context, arguments, 'CWL')
            if 'error' in selected:
                return selected
            if not selected['war_ids']:
                return {'rows': [], 'coverage': selected['coverage'], 'metric': metric}
            scorer = context.cwl_queries.ass_wars if metric == 'ass' else context.cwl_queries.bonus_wars
            snapshot = await asyncio.to_thread(scorer, clan_code=clan[0], war_ids=selected['war_ids'])
            data = asdict(snapshot)
            rows = data.pop('rows' if metric == 'ass' else 'summaries')
            data.pop('attacks', None)
            rows = sorted(rows, key=lambda row: (row.get('rank') is None,
                         row.get('rank') or 0, row['player_tag']))
        else:
            if arguments.get('after') or arguments.get('before') or arguments.get('war_ids') or arguments.get('latest_completed'):
                return {'error': 'Choose ASS or bonus for an explicit subset of wars.', 'cause': 'invalid_arguments',
                        'current_clan_leagues': league_rows}
            snapshot = await asyncio.to_thread(context.cwl_queries.performance,
                history_limit=arguments.get('history_limit', 3), season=arguments.get('season'),
                clan_code=clan[0] if len(clan) == 1 else None)
            data = asdict(snapshot)
            rows = [row for row in data.pop('rows') if not clan or row['clan_code'] in clan]
            season = arguments.get('season')
            complete_groups = {(row['season'], row['clan_code']) for row in rows}
            missing_clans = [code for code in clan or list(CLANS)
                             if season and (season, code) not in complete_groups]
            if missing_clans:
                partial = await selected_wars(context, {**arguments, 'clan_codes': missing_clans}, 'CWL')
                data['missing_sources'] = list(data.get('missing_sources', []))
                if 'error' not in partial:
                    data['seasons'] = tuple(dict.fromkeys((*data.get('seasons', ()), season)))
                    data['attendance_coverage'] = partial['coverage']
                    data['clan_seasons'] = list(data.get('clan_seasons', []))
                for code in missing_clans:
                    if 'error' in partial:
                        data['missing_sources'].append({'source': 'CWL performance', 'clan_code': code,
                            'season': season, 'cause': partial['cause']})
                        continue
                    source_rows = [row for row in partial['rows'] if row['clan_code'] == code]
                    attendance_rows = aggregate_attendance(source_rows, {}, 'account')
                    rows.extend(dict(row, clan_code=code, season=season, score=None, rank=None,
                                     scoring_status='incomplete_season', league=None) for row in attendance_rows)
                    war_ids = {row['war_id'] for row in source_rows}
                    war_ids.update(row['war_id'] for row in partial.get('war_aliases', []) if row['clan_code'] == code)
                    data['clan_seasons'].append({'clan_code': code, 'season': season, 'wars': len(war_ids),
                        'accounts': len(attendance_rows), 'complete': False, 'league': None,
                        'coverage': partial['coverage']})
                    data['missing_sources'].append({'source': 'CWL performance', 'clan_code': code,
                        'season': season, 'cause': 'incomplete_season' if war_ids else 'missing_history'})
            if metric == 'mega_ass':
                unique = {}
                for row in rows:
                    unique.setdefault((row['clan_code'], row['player_tag']), row)
                rows = sorted(unique.values(), key=lambda row: (row.get('multi_season_rank') is None,
                             row.get('multi_season_rank') or 0, row['player_tag']))
        if arguments.get('sheet_by') == 'clan_code' and len(clan) == 1:
            rows = [dict(row, clan_code=clan[0]) for row in rows]
        full_count = len(rows)
        rows = [row for row in rows if not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']]
        unobserved = sorted(set(arguments.get('account_tags', [])) - {row['player_tag'] for row in rows})
        analysis = cwl_analysis(rows, metric, scopes=[{**data,
            'clan_code': clan[0] if len(clan) == 1 else None}])
        if metric == 'mega_ass':
            from elbow_helper.configuration.clans import CLAN_ORDER
            rows.sort(key=lambda row: (CLAN_ORDER.index(row['clan_code']), row.get('multi_season_rank') is None,
                                      row.get('multi_season_rank') or 0, row['player_tag']))
        elif metric not in ('ass', 'bonus'):
            from elbow_helper.configuration.clans import CLAN_ORDER
            rows.sort(key=lambda row: (tuple(data.get('seasons', ())).index(row['season']),
                CLAN_ORDER.index(row['clan_code']), row.get('rank') is None,
                row.get('rank') or 0, row['player_tag']))
        if arguments.get('top_n'):
            rows = rows[:arguments['top_n']]
        return {**data, **analysis, 'rows': rows, 'metric': metric, 'ranked_population': full_count,
                'unobserved_accounts': unobserved,
                **({'current_clan_leagues': league_rows} if league_rows else {}),
                'rank_scope': 'selected clan, season and completed wars before account filtering',
                'coverage': 'stored completed CWL wars; missing seasons remain unknown'}
    except (ValueError, RuntimeError) as error:
        if metric == 'league':
            return {'rows': [], 'current_clan_leagues': league_rows, 'metric': metric,
                    'missing_sources': [{'source': 'CWL history', 'cause': 'source_unavailable'}],
                    'coverage': 'Current profiles only; historical season leagues remain unknown.'}
        return {'error': str(error), 'cause': 'source_unavailable'}


async def current_war(context, arguments):
    codes = arguments.get('clan_codes', list(CLANS))
    source = await read_clash(context, {'kind': 'current_war', 'tags': [CLANS[code].tag for code in codes],
                                       'detail': 'full', 'limit': 50})
    if 'error' in source:
        return source
    rows = []
    by_tag = {item['tag']: item for item in source['items']}
    for code in codes:
        item = by_tag.get(CLANS[code].tag, {'status': 'unavailable'})
        data = item.get('data', {})
        state = data.get('state')
        members = []
        for row in data.get('clan', {}).get('members', []):
            used = len(row.get('attacks', []))
            expected = data.get('attacksPerMember')
            remaining = max(0,expected-used) if type(expected) is int and expected>0 else None
            if arguments.get('account_tags') and row['tag'] not in arguments['account_tags']:
                continue
            members.append({'player_tag': row['tag'], 'player_name': row.get('name'),
                            'map_position': row.get('mapPosition'),
                            **({'attacks': row.get('attacks', [])} if arguments.get('include_attacks') else {}),
                            'attacks_used': used, 'unused_attacks': remaining,
                            'missed': remaining if state == 'warEnded' else None})
        rows.append({'clan_code': code, 'status': item['status'], 'state': state,
                     'start_at': data.get('startTime'), 'end_at': data.get('endTime'),
                     'team_size': data.get('teamSize'), 'opponent': data.get('opponent', {}).get('name'),
                     'score': {side: {key: data.get(side, {}).get(key) for key in ('attacks', 'stars', 'destructionPercentage')}
                               for side in ('clan', 'opponent')},
                     'members': sorted(members, key=lambda row: (row['map_position'] is None,
                         row['map_position'] or 0, row['player_tag'])) if arguments.get('include_roster', True) else []})
    return {'rows': rows, 'coverage': 'current API observation; unused attacks are not misses before war ends'}


TOOLS = (
 family_tool('read_war_attendance', 'Read completed-war participation, missed attacks, stars, triples and destruction per account.',
     {**SELECTION, 'war_type': {'type': 'string', 'enum': ['REG', 'CWL', 'both']},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'sort': {'type': 'string', 'enum': ['missed', 'miss_rate', 'attacks_used', 'attacks_expected']},
      'unit': {'type': 'string', 'enum': ['account', 'member']},
      'member_ids': IDS, 'account_tags': TAGS,
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, attendance, required=('war_type',),
     usage_note='Select season/season_keys, war_ids, latest_completed or dates first. Resolve relative periods from season evidence; account filters do not select a period.'),
 family_tool('read_cwl_results', 'Read current CWL signup/assigned rosters, season results, ASS, bonus and clan leagues.',
     {**SELECTION, 'metric': {'type': 'string', 'enum': ['signup_pool', 'assigned_rosters', 'performance', 'attendance', 'ass', 'mega_ass', 'bonus', 'league'],
                           'description': 'Use league without a historical selector for current clan CWL league labels. The attendance metric is CWL participation and defaults to the latest completed season; performance defaults to three seasons.'},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'history_limit': {'type': 'integer', 'minimum': 1, 'maximum': 12,
                        'description': 'Recent completed seasons for attendance, performance or MEGA ASS. Counts seasons, not wars. Use alone without another period selector.'}, 'account_tags': TAGS,
      'sheet_by': {'type': 'string', 'enum': ['clan_code']},
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, cwl_results,
     usage_note='history_limit counts seasons; latest_completed counts wars. ASS/bonus accept multiple clan_codes; latest per clan. For a tab per clan: sheet_by=clan_code.'),
 family_tool('read_current_war', 'Read current war state and unused attacks for family clans.',
     {'clan_codes': CLANS_INPUT, 'account_tags': TAGS,
      'include_roster': {'type': 'boolean'}, 'include_attacks': {'type': 'boolean'}}, current_war),
)
