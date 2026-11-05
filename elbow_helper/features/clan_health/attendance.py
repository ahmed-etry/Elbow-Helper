"""Attendance totals per account or owner over war lineup rows.

Regular and league war rows both come from the family record.
"""


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
            # A war still under way counts its attacks so far; it has no misses yet.
            if 'not_final' in row.get('uncertainty_reasons', ()):
                item['attacks_used'] += row['attacks_used'] or 0
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
