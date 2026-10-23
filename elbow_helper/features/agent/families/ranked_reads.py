"""Complete account selections and deduplicated observed ranked battle samples."""
import json

from elbow_helper.features.ranked_stats.battles import parse_log
from elbow_helper.domain.player_tags import normalize_player_tag
from .core import family_tool, IDS, TAGS, DATE
from .projections import COHORT_REF, cohort_selection
from .identity_reads import clan_rosters
from ..datasets.clash import read_clash
from ..datasets.ranked import read_ranked_stats, _live_batch
from ..datasets.findings import battle_aggregate
from ..datasets.battle_logs import battle_date
from ..research.history import search_date


def merge_battles(stored, live, tag, after, before):
    """Use the collector's identity; conflicting copies do not enter numeric totals."""
    records, conflicts, duplicates, unreadable = {}, set(), 0, 0
    for source, payload in [('stored', row) for row in stored] + [('live', row) for row in live]:
        observation = {'player_tag':tag, 'member_id':0, 'clan_tag':None,
                       'player_name':None, 'observed_at':before.isoformat()}
        poll, parsed = parse_log({'items':[payload]}, observation, lambda *args:None)
        unreadable += poll['skipped_entries']
        for record in parsed:
            when = battle_date(payload)
            if not after <= when < before:
                continue
            key = record['battle_key']
            if key in records:
                duplicates += 1
                if records[key]['payload'] != payload:
                    conflicts.add(key)
                records[key]['sources'].add(source)
            else:
                records[key] = {'battle_key':key,'payload':payload,'sources':{source}}
    rows = [{**row, 'sources':sorted(row['sources']), 'conflict':key in conflicts}
            for key,row in records.items()]
    rows.sort(key=lambda row:(battle_date(row['payload']),row['battle_key']))
    return rows, duplicates, len(conflicts), unreadable


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
    result = await read_ranked_stats(context, {**selected,'limit':10000}, maximum=10000,
                                     include_battles=True, allow_live_only=True,
                                     selection_maximum=10000 if arguments.get('cohort_ref') or roster_coverage is not None else 200)
    if 'error' in result:
        return result
    if result.get('next_offset') is not None:
        return {'error':'Choose a smaller ranked account cohort; the complete selection exceeds capacity.',
                'cause':'capacity'}
    after = search_date(arguments['after'])
    before = search_date(result['before'])
    accounts = {row['player_tag']:dict(row) for row in result['accounts']}
    live = result.get('live_battle_logs',{}).get('result',{})
    if 'live_battle_logs' not in result and accounts:
        live = await _live_batch(context, 'battle_log', sorted(accounts), full_detail=True)
    if arguments.get('clan_tags'):
        from elbow_helper.configuration.clans import CLANS
        tags = [CLANS[value.upper()].tag if value.upper() in CLANS else normalize_player_tag(value)
                for value in arguments['clan_tags']]
        roster = await read_clash(context, {'kind':'clan','tags':tags,
                    'member_scope':'current','detail':'full','limit':50})
        if 'error' in roster:
            live = roster
        else:
            tags = sorted(set(accounts) | {row['tag'] for clan in roster['items']
                for row in clan.get('data',{}).get('memberList',[]) if row.get('tag')})
            live = await _live_batch(context,'battle_log',tags,full_detail=True) if tags else {'items':[]}
            live['current_clan_statuses'] = [{'clan_tag':clan['tag'],'status':clan['status']}
                                            for clan in roster['items']]
    logs = {row['tag']:row for row in live.get('items',[])}
    stored = {}
    for battle in result.get('stored_battles',[]):
        stored.setdefault(battle['player_tag'],[]).append(json.loads(battle['payload_json']))
    for value in set(logs) | set(arguments.get('account_tags',[])):
        tag = normalize_player_tag(value)
        accounts.setdefault(tag, {'player_tag':tag,'status':'unobserved','attacks_used':None,
            'attacks_available':None,'counts_complete':False,'selection_rank':None,'ranking':{}})
    rows = []
    for tag, account in accounts.items():
        source = logs.get(tag,{'status':'unavailable'})
        merged, duplicates, conflicts, unreadable = merge_battles(stored.get(tag,[]),
            source.get('data',{}).get('items',[]) if source.get('status')=='ok' else [], tag,after,before)
        usable = {'items':[row['payload'] for row in merged if not row['conflict']]}
        attacks = battle_aggregate(usable,'ranked_attacks',after=after,before=before)
        defenses = battle_aggregate(usable,'ranked_defenses',after=after,before=before)
        if source.get('status') != 'ok' and not stored.get(tag):
            attacks = defenses = None
        rows.append({**account,'observed_attacks':attacks,'observed_defenses':defenses,
            'ranked_league':account.get('ranking',{}).get('leagueTier'),
            'live_status':source['status'], 'duplicate_copies_removed':duplicates,
            'conflicting_battles':conflicts, 'unreadable_ranked_rows':unreadable,
            'historical_counts_complete':False,
            **({'battles':merged} if arguments.get('include_battles') else {})})
    rows.sort(key=lambda row:(row.get('selection_rank') is None,row.get('selection_rank') or 0,row['player_tag']))
    return {'rows':rows, 'history_coverage':result.get('history_coverage'),
            'membership_coverage': roster_coverage,
            'collection_limits':result.get('limitations',[]), 'source_complete':result.get('complete',False),
            'live_coverage':{key:value for key,value in live.items() if key!='items'},
            'unobserved_selections':result.get('unobserved_selections',[]),
            'coverage_note':'Observed samples only. Stored and live copies are deduplicated, never summed. '
                'Conflicts are excluded from metrics. Missing collection or a rolling-log boundary cannot prove zero activity. '
                'Stored ownership is first-observed; live clan rosters and bot links are current, not historical.'}


TOOLS = (family_tool('read_ranked_activity','Read ranked activity, complete account cohorts and collection gaps.',
    {'member_ids':IDS,'account_tags':TAGS,'clan_tags':TAGS,'after':DATE,'before':DATE,
     'membership_basis': {'type': 'string', 'enum': ['community', 'current_in_game']},
     'season': {'type': 'string', 'maxLength': 20},
     'include_battles':{'type':'boolean'},'cohort_ref':COHORT_REF},ranked_activity, required=('after',)),)
