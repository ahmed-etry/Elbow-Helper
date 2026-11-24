"""Queryable war facts composed from the existing participation reads."""

from collections import defaultdict
from datetime import datetime, timezone

from elbow_helper.data.store.war_completeness import war_side_coverages

from .core import timestamp
from .wars import final_participation, member_rows, participation_lineup_known


WAR_IDENTITY_COLUMNS = (
    ('war_id', 'INTEGER'), ('clan_tag', 'TEXT'), ('clan_code', 'TEXT'), ('clan_name', 'TEXT'),
    ('opponent_tag', 'TEXT'), ('opponent_name', 'TEXT'), ('type', 'TEXT'), ('state', 'TEXT'),
    ('start_at', 'INTEGER'), ('end_at', 'INTEGER'), ('date', 'TEXT'), ('month', 'TEXT'),
)
WAR_COLUMNS = WAR_IDENTITY_COLUMNS + (
    ('team_size', 'INTEGER'), ('members_saved', 'INTEGER'), ('lineup_complete', 'INTEGER'),
    ('attacks_reported', 'INTEGER'), ('attacks_saved', 'INTEGER'), ('attacks_complete', 'INTEGER'),
    ('counts_final', 'INTEGER'), ('participation_lineup_known', 'INTEGER'),
)
ACCOUNT_WAR_COLUMNS = WAR_IDENTITY_COLUMNS + (
    ('player_tag', 'TEXT'), ('player_name', 'TEXT'), ('townhall', 'INTEGER'), ('map_position', 'INTEGER'),
    ('attacks_expected', 'INTEGER'), ('attacks_used', 'INTEGER'), ('missed', 'INTEGER'),
    ('missed_reason', 'TEXT'), ('stars', 'INTEGER'), ('attacks_measured', 'INTEGER'),
    ('owner_then_id', 'INTEGER'), ('owner_now_id', 'INTEGER'), ('owner_then_reason', 'TEXT'),
    ('owner_from_at', 'INTEGER'), ('owner_to_at', 'INTEGER'), ('owner_basis', 'TEXT'),
)
ACCOUNT_MONTH_COLUMNS = (
    ('player_tag', 'TEXT'), ('month', 'TEXT'), ('owner_now_id', 'INTEGER'), ('wars', 'INTEGER'),
    ('wars_checked', 'INTEGER'), ('wars_with_missing_lineups', 'INTEGER'),
    ('status', 'TEXT'), ('reason', 'TEXT'),
)


def identities(connection, at):
    """Ownership intervals and war identities, shared within one read snapshot."""
    cache = connection.leaf_cache
    key = ('war_fact_identities', at)
    if key not in cache:
        wars = {row['id']: dict(row) for row in connection.execute('SELECT * FROM wars')}
        sides = defaultdict(dict)
        for row in connection.execute(
                'SELECT s.*,c.family_code FROM war_sides s LEFT JOIN clans c ON c.tag=s.clan_tag'):
            sides[row['war_id']][row['clan_tag']] = dict(row)
        links = defaultdict(list)
        for row in connection.execute(
                'SELECT a.player_tag,l.from_at,l.to_at,l.basis,m.discord_id FROM account_links l '
                'JOIN accounts a ON a.id=l.account_id JOIN members m ON m.id=l.member_id '
                'WHERE l.from_at<=? ORDER BY l.from_at', (at,)):
            links[row['player_tag']].append(dict(row))
        cache[key] = wars, sides, links
    return cache[key]


def owner_at(intervals, at):
    return next((row for row in reversed(intervals)
                 if row['from_at'] <= at and (row['to_at'] is None or row['to_at'] > at)), None)


def war_identity(connection, war_id, clan, at):
    wars, sides, _ = identities(connection, at)
    war, pair = wars[war_id], sides[war_id]
    own = pair[clan]
    other = next(row for tag, row in pair.items() if tag != clan)
    date = datetime.fromtimestamp(war['end_at'] / 1_000_000, timezone.utc).date().isoformat()
    return {'war_id': war_id, 'clan_tag': clan, 'clan_code': own['family_code'],
            'clan_name': own['name'], 'opponent_tag': other['clan_tag'], 'opponent_name': other['name'],
            'type': war['type'], 'state': war['state'], 'start_at': war['start_at'],
            'end_at': war['end_at'], 'date': date, 'month': date[:7]}


def account_war_fact(connection, war_id, clan, row, at):
    """Keep the count already decided by participation_rows, with its dated identity."""
    head = war_identity(connection, war_id, clan, at)
    intervals = identities(connection, at)[2].get(row['player_tag'], ())
    then = owner_at(intervals, head['end_at']) if head['end_at'] <= at else None
    now = owner_at(intervals, at)
    reason = None
    if row['missed'] is None:
        reason = ('War has not ended.' if head['state'] != 'warEnded' else
                  'Expected or used attacks were not saved.'
                  if row['attacks_expected'] is None or row['attacks_used'] is None else
                  'Final attack counts are not proven.')
    return {**head, **{name: row[name] for name in (
                'player_tag', 'player_name', 'townhall', 'map_position', 'attacks_expected',
                'attacks_used', 'missed', 'stars', 'attacks_measured')},
            'missed_reason': reason,
            'owner_then_id': then['discord_id'] if then else None,
            'owner_now_id': now['discord_id'] if now else None,
            'owner_then_reason': (None if then else 'War end is after this read.' if head['end_at'] > at
                                  else 'No ownership interval covers this war end.'),
            'owner_from_at': then['from_at'] if then else None,
            'owner_to_at': then['to_at'] if then else None,
            'owner_basis': then['basis'] if then else None}


def month_rows(wars, accounts, links, at):
    """Absence means none of the checked lineups, never continuous historical capture."""
    checked, missing, joined = defaultdict(int), defaultdict(int), defaultdict(set)
    for war in wars:
        checked[war['month']] += 1
        missing[war['month']] += not war['participation_lineup_known']
    for row in accounts:
        joined[row['player_tag'], row['month']].add(row['war_id'])
    present = datetime.fromtimestamp(at / 1_000_000, timezone.utc).strftime('%Y-%m')
    first = min([present, *checked])
    year, month = map(int, first.split('-'))
    months = []
    while (key := f'{year:04d}-{month:02d}') <= present:
        months.append(key)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows = []
    for tag, intervals in sorted(links.items()):
        owner = owner_at(intervals, at)
        for month in months:
            count = len(joined[tag, month])
            status, reason = (
                ('in wars', 'Found in a saved family war lineup.') if count else
                ('unknown', 'No family wars were checked for this month.') if not checked[month] else
                ('unknown', 'Some checked family wars have missing lineups.') if missing[month] else
                ('none found', 'In none of the checked family war lineups; other wars may be absent.'))
            rows.append({'player_tag': tag, 'month': month,
                         'owner_now_id': owner['discord_id'] if owner else None, 'wars': count,
                         'wars_checked': checked[month], 'wars_with_missing_lineups': missing[month],
                         'status': status, 'reason': reason})
    return rows


def build_war_facts(connection, at=None):
    """Build all three tables inside the caller's read-only worker snapshot."""
    from .cwl import CwlReads

    at = timestamp(at)
    selected = [dict(row) for row in connection.execute(
        'SELECT w.*,s.clan_tag FROM wars w JOIN war_sides s ON s.war_id=w.id '
        'JOIN clans c ON c.tag=s.clan_tag WHERE c.is_family=1 AND w.first_seen_at<=?', (at,))]
    coverages = war_side_coverages(connection, {war['id'] for war in selected})
    league = [{**war, 'coverage': coverages[war['id'], war['clan_tag']]}
              for war in selected if war['type'] == 'CWL']
    league_rows = CwlReads._selected_participation_rows(connection, league, None, at) if league else {}
    wars, accounts = [], []
    for war in selected:
        clan = war['clan_tag']
        coverage = coverages[war['id'], clan]
        own = coverage['own']
        if war['type'] == 'CWL':
            rows = league_rows[war['id'], clan]['rows']
            accounts.extend(row['account_war'] for row in rows)
        else:
            rows = member_rows(connection, war, clan, coverage)
            accounts.extend(account_war_fact(connection, war['id'], clan, row, at) for row in rows)
        totals = {'members': len(rows), 'missing_counts': sum(
            row['attacks_expected'] is None or row['attacks_used'] is None for row in rows),
            'used': sum(row['attacks_used'] or 0 for row in rows)}
        extra = identities(connection, at)[1][war['id']][clan]['extra_json']
        wars.append({**war_identity(connection, war['id'], clan, at),
                     'team_size': own['team_size'], 'members_saved': own['members'],
                     'lineup_complete': own['lineup_complete'], 'attacks_reported': own['attacks_reported'],
                     'attacks_saved': own['attacks_stored'], 'attacks_complete': own['attacks_complete'],
                     'counts_final': final_participation(war['state'], coverage, totals, extra),
                     'participation_lineup_known': participation_lineup_known(own)})
    return {'wars': wars, 'account_wars': accounts,
            'account_months': month_rows(wars, accounts, identities(connection, at)[2], at)}
