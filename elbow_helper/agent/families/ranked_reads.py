"""Complete account selections and stored ranked battle samples from the family record."""

from elbow_helper.domain.player_tags import normalize_player_tag
from .core import family_tool, IDS, TAGS, DATE
from .projections import COHORT_REF, cohort_selection
from .identity_reads import clan_rosters
from ..datasets.ranked import read_ranked_stats
from ..datasets.findings import battle_aggregate
from ..research.history import search_date

SELECTOR_ERROR = 'Choose members, accounts or clans.'


async def ranked_activity(context, arguments):
    roster_coverage = None
    if arguments.get('clan_tags') and arguments.get('membership_basis', 'community') == 'community':
        from elbow_helper.configuration.clans import CLANS
        codes = [value.upper() if value.upper() in CLANS else next(
            (code for code, clan in CLANS.items() if normalize_player_tag(value) == clan.tag), None)
            for value in arguments['clan_tags']]
        if all(codes) and any(CLANS[code].cwl_only for code in codes):
            roster = await clan_rosters(context, {'clan_codes': codes, 'basis': 'community',
                **({'season': arguments['season']} if arguments.get('season') else {})})
            if 'error' in roster:
                return roster
            tags = sorted({row['player_tag'] for row in roster['rows'] if row.get('player_tag')})
            if not tags:
                return {'error': 'That clan account cohort is not available.', 'cause': 'source_unavailable'}
            roster_coverage = roster['clans']
            arguments = {**{key: value for key, value in arguments.items() if key != 'clan_tags'}, 'account_tags': tags}
    if arguments.get('cohort_ref'):
        selection = await cohort_selection(context,arguments['cohort_ref'],'account_tags')
        if 'error' in selection:
            return selection
        arguments = {**arguments,**selection}
    selected = {key:value for key,value in arguments.items()
                if key in ('member_ids','account_tags','clan_tags','after','before')}
    result = await read_ranked_stats(context, {**selected,'limit':10000}, maximum=10000, include_battles=True,
                                     selection_maximum=10000 if arguments.get('cohort_ref') or roster_coverage is not None else 200)
    if 'error' in result:
        return result
    if result.get('next_offset') is not None:
        return {'error':'Choose a smaller ranked account cohort; the complete selection exceeds capacity.',
                'cause':'capacity'}
    after = search_date(arguments['after'])
    before = search_date(result['before'])
    rows = []
    for account in result['accounts']:
        battles = account.pop('battles', [])
        usable = {'items': [battle['payload'] for battle in battles]}
        attacks = battle_aggregate(usable,'ranked_attacks',after=after,before=before)
        defenses = battle_aggregate(usable,'ranked_defenses',after=after,before=before)
        if account['attacks_used'] is None:
            # The log was never read and nothing is stored: unknown, not zero.
            attacks = defenses = None
        rows.append({**account,'observed_attacks':attacks,'observed_defenses':defenses,
            'ranked_league':account.get('ranking',{}).get('leagueTier'),
            'watch_status':account['log_coverage']['status'],
            'watched_from':account['log_coverage']['watched_from'],
            'historical_counts_complete':False,
            **({'battles':battles} if arguments.get('include_battles') else {})})
    rows.sort(key=lambda row:(row.get('selection_rank') is None,row.get('selection_rank') or 0,row['player_tag']))
    return {'rows':rows, 'history_coverage':result.get('history_coverage'),
            'membership_coverage': roster_coverage,
            'collection_limits':result.get('limitations',[]), 'source_complete':result.get('complete',False),
            'unobserved_selections':result.get('unobserved_selections',[]),
            'coverage_note':'Stored samples only: battles the family record holds from battle-log reads, '
                'including those the old ranked collector kept. log_coverage gaps show where a log was not read; '
                'a gap or an unread log cannot prove zero activity. Owners are current bot links; a battle\'s clan is '
                'the recorded clan membership at that time, or the clan the old collector first saw it under.'}


TOOLS = (family_tool('read_ranked_activity','Read ranked activity, complete account cohorts and collection gaps.',
    {'member_ids':IDS,'account_tags':TAGS,'clan_tags':TAGS,'after':DATE,'before':DATE,
     'membership_basis': {'type': 'string', 'enum': ['community', 'current_in_game']},
     'season': {'type': 'string', 'maxLength': 20},
     'include_battles':{'type':'boolean'},'cohort_ref':COHORT_REF},ranked_activity, required=('after',),
    # The selector refusals of read_ranked_stats, checked before any read runs.
    rules=({'needs': ['member_ids', 'account_tags', 'clan_tags', 'cohort_ref'], 'error': SELECTOR_ERROR},
           {'only_one': ['member_ids', 'account_tags', 'clan_tags'], 'error': SELECTOR_ERROR},
           {'with': ['cohort_ref'], 'forbids': ['member_ids'], 'error': SELECTOR_ERROR})),)
