"""Shared attack measurements with distinct wars and account-war opportunities."""
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
