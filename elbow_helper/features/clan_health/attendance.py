"""Canonical completed-war attendance from feature-owned stored evidence."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from elbow_helper.domain.player_tags import normalize_player_tag

# Regular wars saved before the wars table only have participation and attacks.
FINAL_PARTICIPATION_SOURCES = ('currentwar_api:warEnded', 'currentwar_api:warEnded_scheduled')
# A Clash pause moves the end time, and with it the war ID, of the same war.
CAPTURE_WINDOW_SECONDS = 72 * 3600


def _identity(war):
    tag = war['war_id'].removeprefix('REG:').removeprefix('CWL:')
    real_tag = normalize_player_tag(tag) if tag.startswith('#') else None
    side = normalize_player_tag(war['clan_tag'])
    if real_tag and side:
        return ('tag', real_tag, side), True
    dimensions = (side, normalize_player_tag(war['opponent_tag']),
                  war['preparation_start_ts'], war['start_ts'],
                  war['team_size'], war['attacks_per_member'])
    if war['war_type'] == 'REG' and all(dimensions):
        return ('regular', *dimensions), True
    return ('unproven', war['clan_code'], war['war_id']), False


def _participation_identity(war_id, clan_tag):
    """Regular war IDs are REG:<clan>:<end time>:<opponent> when Clash gives no war tag."""
    parts = war_id.split(':')
    if len(parts) != 4 or parts[0] != 'REG':
        return None
    side, opponent = normalize_player_tag(parts[1]), normalize_player_tag(parts[3])
    if not side or side != normalize_player_tag(clan_tag) or not opponent:
        return None
    try:
        end = datetime.strptime(parts[2], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return side, opponent, int(end.timestamp())


def _participation_only_wars(connection, clan_codes, stored_wars):
    """Regular wars with final participation but no wars row, one per war.

    Captures within the pause window of each other, or of a stored ended war
    against the same opponent, are the same war; only the latest final
    capture supplies the lineup and attacks.
    """
    activity = [dict(row) for row in connection.execute('''
        SELECT war_id, clan_code, clan_tag, end_ts, player_tag, player_name,
               attacks_expected, attacks_used, source
        FROM war_activity AS activity
        WHERE war_type='REG' AND clan_code IN (SELECT value FROM json_each(?))
          AND NOT EXISTS (SELECT 1 FROM wars WHERE wars.war_id=activity.war_id
                          AND wars.clan_code=activity.clan_code)
        ORDER BY clan_code, war_id, player_tag
    ''', (json.dumps(clan_codes),)).fetchmany(200001)]
    if len(activity) > 200000:
        raise ValueError('Choose a smaller period; the complete attendance snapshot is too large.')
    captures = defaultdict(list)
    for row in activity:
        captures[(row['clan_code'], row['war_id'])].append(row)
    stored_ends = defaultdict(list)
    for war in stored_wars:
        if war['war_type'] == 'REG' and war['end_ts']:
            stored_ends[(war['clan_code'], normalize_player_tag(war['clan_tag']),
                         normalize_player_tag(war['opponent_tag']))].append(war['end_ts'])
    clusters, by_side = [], defaultdict(list)
    for (clan, war_id), rows in captures.items():
        identity = _participation_identity(war_id, rows[0]['clan_tag'])
        if identity is None:
            clusters.append((('unproven', clan, war_id), None, [(None, war_id)]))
        else:
            by_side[(clan, *identity[:2])].append((identity[2], war_id))
    for side, items in by_side.items():
        items.sort()
        cluster = []
        for item in items:
            if cluster and item[0] - cluster[0][0] > CAPTURE_WINDOW_SECONDS:
                clusters.append((('participation', *side, cluster[0][0]), side, cluster))
                cluster = []
            cluster.append(item)
        clusters.append((('participation', *side, cluster[0][0]), side, cluster))
    found, rosters = [], {}
    for key, side, items in clusters:
        clan, war_ids = key[1], [war_id for _, war_id in items]
        final = {war_id: [row for row in captures[(clan, war_id)]
                          if row['source'] in FINAL_PARTICIPATION_SOURCES] for war_id in war_ids}
        final = {war_id: rows for war_id, rows in final.items() if rows}
        if not final:
            continue
        if side is not None and any(abs(stored - end) <= CAPTURE_WINDOW_SECONDS
                                    for stored in stored_ends[side] for end, _ in items):
            continue
        chosen = max(final, key=lambda war_id: (max(row['end_ts'] for row in final[war_id]), war_id))
        lineup = final[chosen]
        rosters[(clan, chosen)] = [{**row, 'roster_state': 'warEnded', 'captured_ts': row['end_ts']}
                                   for row in lineup]
        war = {'war_id': chosen, 'war_type': 'REG', 'clan_code': clan, 'clan_tag': lineup[0]['clan_tag'],
               'opponent_tag': side[2] if side else None, 'cwl_season': '',
               'preparation_start_ts': None, 'start_ts': None,
               'end_ts': max(row['end_ts'] for row in lineup)}
        found.append((key, {'identity_proven': side is not None, 'war': war,
            'wars': [{'war_id': war_id, 'source': source} for war_id in war_ids
                     for source in sorted({row['source'] for row in captures[(clan, war_id)]})],
            'aliases': sorted(war_ids), 'evidence': [chosen]}))
    return found, rosters


def attendance_snapshot(path, *, clan_codes, war_type, after_ts=None, before_ts=None,
                        season=None, war_ids=(), latest_completed=None, season_keys=(), history_limit=None):
    if history_limit is not None:
        if war_type != 'CWL' or type(history_limit) is not int or not 1 <= history_limit <= 12:
            raise ValueError('Invalid CWL history limit')
        if season or season_keys or war_ids or latest_completed or after_ts is not None or before_ts is not None:
            raise ValueError('Choose recent seasons or an explicit period, not both.')
    kinds = ['REG', 'CWL'] if war_type == 'both' else [war_type]
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        wars = [dict(row) for row in connection.execute('''
            SELECT * FROM wars WHERE state='warEnded'
              AND war_type IN (SELECT value FROM json_each(?))
              AND clan_code IN (SELECT value FROM json_each(?))
            ORDER BY last_seen_ts, war_id
        ''', (json.dumps(kinds), json.dumps(clan_codes))).fetchmany(5001)]
        if len(wars) > 5000:
            raise ValueError('Choose fewer clans; stored war identities exceed the snapshot bound.')
        groups = {}
        for war in wars:
            key, proven = _identity(war)
            group = groups.setdefault(key, {'wars': [], 'identity_proven': proven})
            group['wars'].append(war)
        reconciled = []
        for key, group in groups.items():
            war = max(group['wars'], key=lambda row: (row['last_seen_ts'], row['war_id']))
            reconciled.append((key, {**group, 'war': war,
                'aliases': sorted({row['war_id'] for row in group['wars']})}))
        participation_rosters = {}
        if 'REG' in kinds:
            participation_wars, participation_rosters = _participation_only_wars(connection, clan_codes, wars)
            if len(wars) + len(participation_wars) > 5000:
                raise ValueError('Choose fewer clans; stored war identities exceed the snapshot bound.')
            reconciled.extend(participation_wars)
        component_seasons = list(season_keys)
        if history_limit is not None:
            by_season_clan = defaultdict(list)
            for _, group in reconciled:
                war = group['war']
                if war['cwl_season']:
                    by_season_clan[(war['cwl_season'], war['clan_code'])].append(war)
            completed = {}
            for (key, _), observations in by_season_clan.items():
                if len(observations) >= 7:
                    completed[key] = max(completed.get(key, 0), max(war['end_ts'] for war in observations))
            component_seasons = sorted(completed, key=lambda key: (completed[key], key), reverse=True)[:history_limit]
        selected = []
        requested = set(war_ids)
        for key, group in reconciled:
            war, aliases = group['war'], group['aliases']
            if (after_ts is not None and war['end_ts'] < after_ts
                    or before_ts is not None and war['end_ts'] >= before_ts
                    or season is not None and war['cwl_season'] != season
                    or (season_keys or history_limit is not None) and war['cwl_season'] not in component_seasons
                    or requested and not requested.intersection(aliases)):
                continue
            selected.append((key, group))
        selected.sort(key=lambda item: (-item[1]['war']['end_ts'], item[1]['war']['clan_code'], item[1]['war']['war_id']))
        if latest_completed is not None:
            selected = selected[:latest_completed]
        aliases = sorted({alias for _, group in selected for alias in group['aliases']})
        params = (json.dumps(clan_codes), json.dumps(aliases))
        rosters = [dict(row) for row in connection.execute('''
            SELECT * FROM war_roster_members
            WHERE clan_code IN (SELECT value FROM json_each(?))
              AND war_id IN (SELECT value FROM json_each(?))
        ''', params).fetchmany(200001)]
        attacks = [dict(row) for row in connection.execute('''
            SELECT * FROM war_attacks
            WHERE clan_code IN (SELECT value FROM json_each(?))
              AND war_id IN (SELECT value FROM json_each(?)) AND war_state='warEnded'
        ''', params).fetchmany(200001)]
        if len(rosters) > 200000 or len(attacks) > 200000:
            raise ValueError('Choose a smaller period; the complete attendance snapshot is too large.')
    roster_by_war, attacks_by_war = defaultdict(list), defaultdict(list)
    for row in rosters:
        roster_by_war[(row['clan_code'], row['war_id'])].append(row)
    roster_by_war.update(participation_rosters)
    for row in attacks:
        attacks_by_war[(row['clan_code'], row['war_id'])].append(row)
    rows, empty_wars, conflicts, uncertain_ids, alias_sources = [], [], [], [], []
    for _, group in selected:
        war, aliases = group['war'], group['aliases']
        evidence = group.get('evidence', aliases)
        clan = war['clan_code']
        reasons = []
        if not group['identity_proven']:
            reasons.append('unproven_war_identity')
            uncertain_ids.append(war['war_id'])
        observations, final_rosters = {}, []
        for alias in evidence:
            roster = roster_by_war[(clan, alias)]
            final = [row for row in roster if row['roster_state'] == 'warEnded']
            if final:
                final_rosters.append({(row['player_tag'], row['attacks_expected']) for row in final})
            for row in roster:
                previous = observations.get(row['player_tag'])
                order = (row['roster_state'] == 'warEnded', row['captured_ts'], row['war_id'])
                if previous is None or order > (previous['roster_state'] == 'warEnded', previous['captured_ts'], previous['war_id']):
                    observations[row['player_tag']] = row
        if final_rosters and any(value != final_rosters[0] for value in final_rosters[1:]):
            reasons.append('conflicting_final_rosters')
        if not observations:
            empty_wars.append(war['war_id'])
        merged_attacks, orders = {}, {}
        for alias in evidence:
            for attack in attacks_by_war[(clan, alias)]:
                order = attack['attack_order']
                identity = (order, attack['player_tag'], attack['defender_tag'])
                signature = tuple(attack.get(field) for field in
                    ('stars', 'destruction', 'defender_townhall', 'fresh_attack', 'duration'))
                if (identity in merged_attacks and merged_attacks[identity] != signature
                        or order in orders and orders[order] != identity):
                    reasons.append('conflicting_final_attacks')
                merged_attacks[identity] = signature
                orders[order] = identity
        used_by_tag = Counter(identity[1] for identity in merged_attacks)
        if reasons:
            conflicts.append({'war_id': war['war_id'], 'reasons': sorted(set(reasons))})
        alias_sources.append({'war_id': war['war_id'], 'clan_code': clan, 'season': war['cwl_season'], 'aliases': aliases,
                              'sources': sorted({row['source'] for row in group['wars']})})
        for tag, row in sorted(observations.items()):
            measured = [signature for identity, signature in merged_attacks.items()
                        if identity[1] == tag and not reasons]
            stars = [value[0] for value in measured if type(value[0]) is int and 0 <= value[0] <= 3]
            destruction = [value[1] for value in measured if type(value[1]) in (int, float)
                           and 0 <= value[1] <= 100]
            expected = row['attacks_expected']
            used = used_by_tag.get(tag, 0) if merged_attacks else row['attacks_used']
            complete = (not reasons and row['roster_state'] == 'warEnded'
                        and type(expected) is int and expected > 0
                        and type(used) is int and 0 <= used <= expected)
            rows.append({'war_id': war['war_id'], 'war_aliases': aliases, 'clan_code': clan,
                'war_type': war['war_type'], 'season': war['cwl_season'],
                'start_ts': war['start_ts'], 'end_ts': war['end_ts'],
                'player_tag': tag, 'player_name': row['player_name'],
                'attacks_expected': expected if complete else None,
                'attacks_used': used, 'missed': expected - used if complete else None,
                'final_capture': complete, 'uncertainty_reasons': sorted(set(reasons)),
                'observed_attack_performance': {'attacks': len(measured), 'stars': sum(stars),
                    'stars_measured': len(stars), 'triples': sum(value == 3 for value in stars),
                    'destruction_total': sum(destruction), 'destruction_measured': len(destruction)}})
    return {'rows': rows, 'war_ids': sorted({group['war']['war_id'] for _, group in selected}),
        **({'component_seasons': component_seasons,
            'clan_seasons': [{'season': key, 'clan_code': code,
                'wars': sum(group['war']['clan_code'] == code and group['war']['cwl_season'] == key
                            for _, group in selected)} for key in component_seasons for code in clan_codes]}
           if season_keys or history_limit is not None else {}),
        'war_aliases': alias_sources, 'wars_without_rosters': sorted(empty_wars),
        'identity_conflicts': conflicts, 'unproven_war_ids': sorted(uncertain_ids),
        'duplicate_identities_removed': sum(len(group['aliases']) - 1 for _, group in selected),
        'coverage': {'query_complete': True, 'historical_collection_complete': False,
                     'exact_ranking': not (conflicts or empty_wars),
                     'regular_wars_not_checked_for_friendly': sum(
                         group['war']['war_type'] == 'REG'
                         and not (group['war']['preparation_start_ts'] and group['war']['start_ts'])
                         for _, group in selected),
                     'basis': 'stored completed wars; aliases are reconciled before period selection'}}


def aggregate_attendance(rows, owners, unit):
    grouped = {}
    for row in rows:
        tag = row['player_tag']
        owner = owners.get(tag)
        key = ('member', owner) if unit == 'member' and owner else ('account', tag)
        item = grouped.setdefault(key, {'member_id': owner if unit == 'member' else None,
            'player_tag': tag if key[0] == 'account' else None,
            'player_name': row['player_name'] if key[0] == 'account' else None,
            'account_tags': set(), 'war_ids': set(), 'attacks_used': 0,
            'attacks_expected': 0, 'missed': 0, 'unconfirmed_opportunities': 0})
        item['account_tags'].add(tag)
        item['war_ids'].add(row['war_id'])
        if row['missed'] is None:
            item['unconfirmed_opportunities'] += 1
        else:
            item['attacks_expected'] += row['attacks_expected']
            item['attacks_used'] += row['attacks_used']
            item['missed'] += row['missed']
    result = []
    for key, item in grouped.items():
        item.update(account_tags=sorted(item['account_tags']), war_ids=sorted(item['war_ids']),
                    unit=key[0], missing_link=unit == 'member' and key[0] == 'account')
        item['miss_rate'] = item['missed'] / item['attacks_expected'] if item['attacks_expected'] else None
        item['totals_complete'] = not item['unconfirmed_opportunities']
        result.append(item)
    return sorted(result, key=lambda row: (-row['missed'], -row['attacks_expected'],
                                           row['member_id'] or 0, row['player_tag'] or ''))
