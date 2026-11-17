"""Typed completed-war participation, CWL scoring and current-war evidence."""
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.cwl import SCORED_WARS_LIMIT, SEASON_KEY
from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.features.cwl.results import CwlResults
from elbow_helper.agent.reads.snapshots import family_tool, IDS, TAGS, DATE
from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.agent.reads.cache import stored_read
from .signups import LEAGUE_MONTHS_ONLY, WAR_SELECTORS as ROSTER_WAR_SELECTORS, cwl_rosters
from .wars import participation_totals, current_war, period, selected_wars, single_selection, war_participation
from .war_attacks import war_attacks
from .war_measures import clan_participation, participation_status

__all__ = ['TOOLS', 'participation_totals', 'current_war', 'period', 'selected_wars', 'single_selection',
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
PARTICIPATION_PERIOD_NOTE = 'Without accounts, members or a cohort, give season, season_keys, latest_completed, after/before or war_ids; otherwise all their stored wars are read.'
SELECTION_RULES = (
    {'only_one': [key for key in WAR_SELECTORS if key != 'latest_completed'], 'error': ONE_SELECTION},
    {'only_one': [key for key in WAR_SELECTORS if key != 'season'], 'error': ONE_SELECTION})
PARTICIPATION_RULES = (*WINDOW_RULES,
    *SELECTION_RULES,
    {'unless': ['account_tags', 'member_ids', 'cohort_ref'],
     'needs': WAR_SELECTORS, 'error': 'Which season, wars or date window do you want?'})
ATTACK_RULES = (*PARTICIPATION_RULES,
    {'only_one': ['account_tags', 'member_ids', 'cohort_ref'], 'error': 'Choose accounts, members or a cohort, not several.'})
CWL_RULES = (*WINDOW_RULES,
    {'when': {'metric': ['ass', 'bonus']}, 'forbids': ['history_limit'],
     'error': 'ASS and bonus do not support history_limit. Use season (YYYY-MM) for one season, including the running one, season_keys for several seasons, or war_ids, latest_completed or after/before for part of a season.'},
    *({'when': {'metric': ['ass', 'bonus', 'participation']},
       'only_one': [*rule['only_one'], 'history_limit'], 'error': ONE_SELECTION} for rule in SELECTION_RULES),
    # Performance is the default metric, so an absent metric is checked too. A season may be
    # narrowed to its newest finished wars with latest_completed; every other pair conflicts.
    {'when': {'metric': ['performance', None]}, 'error': ONE_SELECTION,
     'only_one': ['season', 'season_keys', 'war_ids', 'after', 'history_limit']},
    {'when': {'metric': ['performance', None]}, 'error': ONE_SELECTION,
     'only_one': ['latest_completed', 'season_keys', 'war_ids', 'after', 'history_limit']},
    {'when': {'delivery': [None, 'text', 'csv']}, 'forbids': ['sheet_by'],
     'error': 'sheet_by applies only with delivery=xlsx.'},
    {'when': {'metric': ['ass', 'bonus'], 'latest_completed': list(range(
        SCORED_WARS_LIMIT + 1, SELECTION['latest_completed']['maximum'] + 1))}, 'forbids': ['latest_completed'],
     'error': f'ASS and bonus score at most {SCORED_WARS_LIMIT} wars at once; choose fewer.'},
    {'when': {'metric': ['ass', 'bonus']}, 'needs': ['clan_codes'], 'error': 'Which clan should I score?'},
    {'when': {'metric': ['ass', 'bonus']}, 'within': {'clan_codes': list(CWL_CLAN_CODES)},
     'error': 'Not CWL clans: {values}.'},
    {'when': {'metric': ['signup_pool', 'assigned_rosters']}, 'forbids': list(ROSTER_WAR_SELECTORS),
     'error': LEAGUE_MONTHS_ONLY},
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
        return await cwl_rosters(context, arguments)
    if metric == 'league':
        return await cwl_leagues(context, arguments)
    refused = CwlResults.refusal(arguments)
    if refused:
        return refused
    if context.cwl_queries is None:
        return {'error': 'CWL results are not available.', 'cause': 'missing_setup'}
    if metric == 'participation' or metric == 'performance' and any(arguments.get(key)
            for key in ('after', 'before', 'war_ids', 'latest_completed')):
        if metric == 'participation' and not any(arguments.get(key) for key in
                ('season', 'season_keys', 'history_limit', 'war_ids', 'latest_completed', 'after', 'before')):
            arguments = {**arguments, 'history_limit': 1}
        result = await participation_totals(context, {**arguments, 'war_type': 'CWL'},
                                  separate_seasons=metric == 'participation')
        return {**result, 'metric': metric,
                'period_unit': 'season' if any(arguments.get(key) for key in ('season', 'season_keys', 'history_limit'))
                    else 'war' if arguments.get('latest_completed') or arguments.get('war_ids') else 'date_window',
                **({'season': arguments['season']} if arguments.get('season') else {}),
                **({'requested_seasons': arguments['history_limit']} if arguments.get('history_limit') else {})}
    return await stored_read(context, context.cwl_queries, 'results', **arguments)


async def cwl_leagues(context, arguments):
    codes = arguments.get('clan_codes', [])
    league_rows = await current_leagues(context, codes or list(CLANS))
    if not any(arguments.get(key) for key in ('season', 'season_keys', 'history_limit', 'after', 'before', 'war_ids', 'latest_completed')):
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
 family_tool('read_war_participation', 'Read completed-war participation, missed attacks, stars, triples and destruction per account.',
     {**SELECTION, 'war_type': {'type': 'string', 'enum': ['REG', 'CWL', 'both']},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'sort': {'type': 'string', 'enum': ['missed', 'miss_rate', 'attacks_used', 'attacks_expected']},
      'unit': {'type': 'string', 'enum': ['account', 'member']},
      'member_ids': IDS, 'account_tags': TAGS,
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, war_participation, required=('war_type',),
     rules=PARTICIPATION_RULES,
     usage_note=PARTICIPATION_PERIOD_NOTE, whole=True),
 family_tool('read_war_attacks', 'Read individual stored war attacks: map positions and Town Halls, stars, destruction and order.',
     {**SELECTION, 'war_type': {'type': 'string', 'enum': ['REG', 'CWL', 'both']},
      'member_ids': IDS, 'account_tags': TAGS,
      'cohort_ref': {'type': 'string', 'minLength': 1, 'maxLength': 32}}, war_attacks, required=('war_type',),
     rules=ATTACK_RULES,
     usage_note='Select the period as for read_war_participation.', whole=True),
 family_tool('read_cwl_results', 'Read CWL signup/assigned rosters, season results, ASS (7-attack projection), bonus and clan leagues.',
     {**SELECTION, 'metric': {'type': 'string', 'enum': ['signup_pool', 'assigned_rosters', 'performance', 'participation', 'ass', 'mega_ass', 'bonus', 'league']},
      'top_n': {'type': 'integer', 'minimum': 1, 'maximum': 500},
      'history_limit': {'type': 'integer', 'minimum': 1, 'maximum': 12,
                        'description': 'Recent completed seasons for participation, performance, MEGA ASS or league. Counts seasons, not wars. Use alone without another period selector.'}, 'account_tags': TAGS,
      'sheet_by': {'type': 'string', 'enum': ['clan_code']},
      'cohort_ref':{'type':'string','minLength':1,'maxLength':32}}, cwl_results, rules=CWL_RULES,
     usage_note='No period: 3 ended seasons, +latest for account performance; participation, ASS/bonus: latest; league: current. history_limit: seasons; latest_completed: wars.',
     whole=True),
 family_tool('read_current_war', 'Read current war state and unused attacks for family clans.',
     {'clan_codes': CLANS_INPUT, 'account_tags': TAGS,
      'include_roster': {'type': 'boolean'}, 'include_attacks': {'type': 'boolean'}}, current_war, whole=True),
)
