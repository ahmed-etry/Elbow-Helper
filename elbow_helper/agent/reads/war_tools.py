"""Typed completed-war attendance, CWL scoring and current-war evidence."""
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.features.cwl.results import CwlResults
from elbow_helper.agent.reads.snapshots import family_tool, IDS, TAGS, DATE
from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.agent.reads.cache import stored_read
from .wars import attendance, current_war, period, selected_wars, single_selection
from .war_measures import clan_participation, participation_status

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
    {'when': {'metric': ['ass', 'bonus']}, 'forbids': ['history_limit'],
     'error': 'ASS and bonus do not support history_limit. Use season (YYYY-MM) for one season, including the running one, season_keys for several seasons, or war_ids, latest_completed or after/before for part of a season.'},
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
        selected = await cohort_selection(context, arguments['cohort_ref'], 'account_tags')
        if 'error' in selected:
            return selected
        arguments = {**{key: value for key, value in arguments.items() if key != 'cohort_ref'}, **selected}
    metric = arguments.get('metric', 'performance')
    if arguments.get('after') and not arguments.get('before'):
        arguments = {**arguments, 'before': context.source_message.created_at.isoformat()}
    if metric in ('signup_pool', 'assigned_rosters'):
        from .signups import cwl_rosters
        return await cwl_rosters(context, arguments)
    if metric == 'league':
        return await cwl_leagues(context, arguments)
    refused = CwlResults.refusal(arguments)
    if refused:
        return refused
    if context.cwl_queries is None:
        return {'error': 'CWL results are not available.', 'cause': 'missing_setup'}
    if metric == 'attendance' or metric == 'performance' and any(arguments.get(key)
            for key in ('after', 'before', 'war_ids', 'latest_completed')):
        if metric == 'attendance' and not any(arguments.get(key) for key in
                ('season', 'season_keys', 'history_limit', 'war_ids', 'latest_completed', 'after', 'before')):
            arguments = {**arguments, 'history_limit': 1}
        result = await attendance(context, {**arguments, 'war_type': 'CWL'},
                                  separate_seasons=metric == 'attendance')
        return {**result, 'metric': metric,
                'period_unit': 'season' if any(arguments.get(key) for key in ('season', 'season_keys', 'history_limit'))
                    else 'war' if arguments.get('latest_completed') or arguments.get('war_ids') else 'date_window',
                **({'season': arguments['season']} if arguments.get('season') else {}),
                **({'requested_seasons': arguments['history_limit']} if arguments.get('history_limit') else {})}
    return await stored_read(context, context.cwl_queries, 'results', **arguments)


async def cwl_leagues(context, arguments):
    codes = arguments.get('clan_codes', [])
    league_rows = await current_leagues(context, codes or list(CLANS))
    if not any(arguments.get(key) for key in ('season', 'season_keys', 'after', 'before', 'war_ids', 'latest_completed')):
        return {'rows': league_rows, 'current_clan_leagues': league_rows, 'metric': 'league',
                'coverage': 'Current clan profile league labels; participation is not inferred.'}
    if context.cwl_queries is None:
        return {'rows': [], 'current_clan_leagues': league_rows, 'metric': 'league',
                'missing_sources': [{'source': 'CWL history', 'cause': 'missing_setup'}],
                'coverage': 'Current profiles only; historical season leagues remain unknown.'}
    result = await stored_read(context, context.cwl_queries, 'results', **arguments)
    if result.get('cause') == 'invalid_arguments':
        return {**result, 'current_clan_leagues': league_rows}
    if 'error' in result:
        return {'rows': [], 'current_clan_leagues': league_rows, 'metric': 'league',
                'missing_sources': [{'source': 'CWL history', 'cause': 'source_unavailable'}],
                'coverage': 'Current profiles only; historical season leagues remain unknown.'}
    return {**result, 'current_clan_leagues': league_rows}


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
