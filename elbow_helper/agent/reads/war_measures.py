"""Shared attack measurements with distinct wars and account-war opportunities."""
from collections import defaultdict
from ..datasets.findings import ratio


def aggregate_performance(opportunities):
    totals = dict.fromkeys(('attacks', 'stars', 'stars_measured', 'triples',
                           'destruction_total', 'destruction_measured'), 0)
    wars, accounts, count = set(), set(), 0
    for row in opportunities:
        count += 1
        wars.add(row['war_id'])
        accounts.add(row['player_tag'])
        measured = row.get('observed_attack_performance', {})
        for field in totals:
            totals[field] += measured.get(field, 0)
    return {'wars': len(wars), 'account_war_opportunities': count,
        'accounts': sorted(accounts), 'observed_attacks': totals['attacks'],
        'stars': totals['stars'], 'stars_measured': totals['stars_measured'], 'triples': totals['triples'],
        'average_stars': ratio(totals['stars'], totals['stars_measured']),
        'triple_rate_percent': ratio(100 * totals['triples'], totals['stars_measured'], 2),
        'average_destruction_percent': ratio(totals['destruction_total'], totals['destruction_measured'], 2),
        'measurement_status': 'observed' if totals['attacks'] else 'no_attack_measurements'}


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


