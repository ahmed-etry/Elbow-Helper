"""Typed completed-war attendance, CWL scoring and current-war evidence."""
from dataclasses import asdict

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from .core import family_tool, IDS, TAGS, DATE
from ..datasets.clash import read_clash
from .cwl_summary import cwl_analysis
from ..read_cache import stored_read
from ..reads import wars as war_adapter
from ..reads.wars import attendance, current_war, period, selected_wars, single_selection
from ..reads.war_measures import clan_participation, participation_status

__all__ = ['TOOLS', 'attendance', 'current_war', 'period', 'selected_wars', 'single_selection',
           'participation_status', 'clan_participation', 'cwl_results']

CLANS_INPUT = {'type': 'array', 'minItems': 1, 'maxItems': 9, 'uniqueItems': True,
               'items': {'type': 'string', 'enum': list(CLANS)}}
SEASON = {'type': 'string', 'maxLength': 20, 'pattern': f'^{SEASON_KEY}$'}
SELECTION = {'clan_codes': CLANS_INPUT, 'season': SEASON,
             'season_keys': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                             'uniqueItems': True, 'items': SEASON},
             'latest_completed': {'type': 'integer', 'minimum': 1, 'maximum': 100,
                                  'description': 'Number of individual completed wars across the selected clans, not seasons. Use only for a requested war subset; omit for season ASS/bonus.'},
             'after': DATE, 'before': DATE, 'war_ids': {'type': 'array', 'minItems': 1,
             'maxItems': 56, 'items': {'type': 'string', 'maxLength': 100}}}
# The same refusals as period() and single_selection(), checked before any read runs.
WINDOW_RULES = (
    {'with': ['before'], 'needs': ['after'], 'error': 'Choose a complete date window with its end after its start.'},
    {'aware': ['after', 'before'], 'error': 'Choose timezone-aware dates.'})
WAR_SELECTORS = ['season', 'season_keys', 'war_ids', 'latest_completed', 'after']
ONE_SELECTION = 'Choose one season selection, war selection or date window.'
HISTORICAL = ['season', 'season_keys', 'after', 'before', 'war_ids', 'latest_completed', 'history_limit']
ATTENDANCE_RULES = (*WINDOW_RULES,
    {'only_one': WAR_SELECTORS, 'error': ONE_SELECTION},
    {'needs': WAR_SELECTORS, 'error': 'Which season, wars or date window do you want?'})
CWL_RULES = (*WINDOW_RULES,
    {'when': {'metric': ['ass', 'bonus', 'attendance']}, 'only_one': [*WAR_SELECTORS, 'history_limit'],
     'error': ONE_SELECTION},
    {'when': {'metric': ['ass', 'bonus']}, 'needs': ['clan_codes'], 'error': 'Which clan should I score?'},
    {'when': {'metric': ['ass', 'bonus']}, 'within': {'clan_codes': list(CWL_CLAN_CODES)},
     'error': 'Not CWL clans: {values}.'},
    {'when': {'metric': ['signup_pool', 'assigned_rosters']}, 'forbids': HISTORICAL,
     'error': 'Current roster cycles do not support historical selectors.'},
    {'when': {'metric': ['mega_ass']}, 'forbids': ['season_keys'],
     'error': 'Choose the MEGA ASS history depth rather than separate season scores.'},
    {'when': {'metric': ['mega_ass', 'league']}, 'forbids': ['after', 'before', 'war_ids', 'latest_completed'],
     'error': 'Choose ASS or bonus for an explicit subset of wars.'})


def cwl_clan_error(codes):
    """A planner error naming selected clans that take no part in CWL."""
    unknown = [code for code in codes if code not in CWL_CLAN_CODES]
    if unknown:
        return {'error': 'Not CWL clans: ' + ', '.join(unknown) + '.', 'cause': 'invalid_arguments',
                'invalid_fields': ['clan_codes']}


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
    if metric in ('ass', 'bonus') or metric in ('performance', 'mega_ass') and len(clan) == 1:
        refused = cwl_clan_error(clan)
        if refused:
            return refused
    if metric in ('ass', 'bonus') and len(clan) > 1:
        # Options every clan would refuse alike are one mistake to correct, not a gap per clan.
        try:
            single_selection(context, arguments)
        except ValueError as error:
            return {'error': str(error), 'cause': 'invalid_arguments'}
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
        if gaps and not summaries and all(gap['cause'] == 'invalid_arguments' for gap in gaps):
            return {'error': gaps[0]['error'], 'cause': 'invalid_arguments'}
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
                return {'error': 'Which clan should I score?', 'cause': 'invalid_arguments', 'invalid_fields': ['clan_codes']}
            if not any(arguments.get(key) for key in (
                    'season', 'after', 'before', 'war_ids', 'latest_completed')):
                season = await stored_read(context, context.cwl_queries, 'latest_season', clan_code=clan[0])
                if season is None:
                    return {'rows': [], 'metric': metric, 'seasons': [],
                            'coverage': 'No stored completed CWL wars for the selected clan.'}
                arguments = {**arguments, 'season': season}
            selected = await war_adapter.selected_wars(context, arguments, 'CWL')
            if 'error' in selected:
                return selected
            if not selected['war_ids']:
                return {'rows': [], 'coverage': selected['coverage'], 'metric': metric}
            snapshot = await stored_read(context, context.cwl_queries,
                'ass_wars' if metric == 'ass' else 'bonus_wars', clan_code=clan[0], war_ids=selected['war_ids'])
            data = asdict(snapshot)
            rows = data.pop('rows' if metric == 'ass' else 'summaries')
            data.pop('attacks', None)
            rows = sorted(rows, key=lambda row: (row.get('rank') is None,
                         row.get('rank') or 0, row['player_tag']))
        else:
            if arguments.get('after') or arguments.get('before') or arguments.get('war_ids') or arguments.get('latest_completed'):
                return {'error': 'Choose ASS or bonus for an explicit subset of wars.', 'cause': 'invalid_arguments',
                        'current_clan_leagues': league_rows}
            snapshot = await stored_read(context, context.cwl_queries, 'performance',
                history_limit=arguments.get('history_limit', 3), season=arguments.get('season'),
                clan_code=clan[0] if len(clan) == 1 else None)
            data = asdict(snapshot)
            rows = [row for row in data.pop('rows') if not clan or row['clan_code'] in clan]
            season = arguments.get('season')
            complete_groups = {(row['season'], row['clan_code']) for row in rows}
            missing_clans = [code for code in clan or list(CLANS)
                             if season and (season, code) not in complete_groups]
            if missing_clans:
                partial = await war_adapter.selected_wars(context, {**arguments, 'clan_codes': missing_clans}, 'CWL')
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


TOOLS = (
 family_tool('read_war_attendance', 'Read completed-war participation, missed attacks, stars, triples and destruction per account.',
     {**SELECTION, 'war_type': {'type': 'string', 'enum': ['REG', 'CWL', 'both']},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'sort': {'type': 'string', 'enum': ['missed', 'miss_rate', 'attacks_used', 'attacks_expected']},
      'unit': {'type': 'string', 'enum': ['account', 'member']},
      'member_ids': IDS, 'account_tags': TAGS,
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, attendance, required=('war_type',),
     rules=ATTENDANCE_RULES,
     usage_note='Select season/season_keys, war_ids, latest_completed or dates first. Resolve relative periods from season evidence; account filters do not select a period.'),
 family_tool('read_cwl_results', 'Read current CWL signup/assigned rosters, season results, ASS, bonus and clan leagues.',
     {**SELECTION, 'metric': {'type': 'string', 'enum': ['signup_pool', 'assigned_rosters', 'performance', 'attendance', 'ass', 'mega_ass', 'bonus', 'league'],
                           'description': 'Use league without a historical selector for current clan CWL league labels. The attendance metric is CWL participation and defaults to the latest completed season; performance defaults to three seasons.'},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'history_limit': {'type': 'integer', 'minimum': 1, 'maximum': 12,
                        'description': 'Recent completed seasons for attendance, performance or MEGA ASS. Counts seasons, not wars. Use alone without another period selector.'}, 'account_tags': TAGS,
      'sheet_by': {'type': 'string', 'enum': ['clan_code']},
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, cwl_results, rules=CWL_RULES,
     usage_note='history_limit counts seasons; latest_completed counts wars. ASS/bonus accept multiple clan_codes; latest per clan. For a tab per clan: sheet_by=clan_code.'),
 family_tool('read_current_war', 'Read current war state and unused attacks for family clans.',
     {'clan_codes': CLANS_INPUT, 'account_tags': TAGS,
      'include_roster': {'type': 'boolean'}, 'include_attacks': {'type': 'boolean'}}, current_war),
)
