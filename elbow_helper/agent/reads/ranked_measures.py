"""Exact measures over collected ranked battles, independent of report paging."""

from collections import defaultdict
from decimal import Decimal
import math

from ..research.history import search_date

METRICS = ('average_stars', 'triple_rate_percent', 'average_destruction_percent')
MODES = {'ranked_and_legend': ('RANKED', 'LEGEND'), 'ranked': ('RANKED',), 'legend': ('LEGEND',)}
TOTALS = ('battles', 'stars_total', 'triples', 'stars_denominator', 'destruction_denominator',
          'destruction_total', 'missing_stars', 'missing_destruction')


def ratio(numerator, denominator, places=4):
    value = Decimal(str(numerator)) / denominator if denominator else None
    return {'numerator': numerator, 'denominator': denominator,
            'value': float(value) if value is not None else None,
            'display': format(value, f'.{places}f') if value is not None else 'unavailable'}


def ratios(totals):
    return {**totals, 'average_stars': ratio(totals['stars_total'], totals['stars_denominator']),
            'triple_rate_percent': ratio(100 * totals['triples'], totals['stars_denominator'], 2),
            'average_destruction_percent': ratio(totals['destruction_total'],
                                                  totals['destruction_denominator'], 2)}


def metrics(rows):
    stars = [row['stars'] for row in rows if type(row.get('stars')) is int and 0 <= row['stars'] <= 3]
    destruction = [Decimal(str(row['destructionPercentage'])) for row in rows
                   if type(row.get('destructionPercentage')) in (int, float)
                   and math.isfinite(row['destructionPercentage']) and 0 <= row['destructionPercentage'] <= 100]
    return ratios({'battles': len(rows), 'stars_total': sum(stars), 'triples': sum(value == 3 for value in stars),
                   'stars_denominator': len(stars), 'destruction_denominator': len(destruction),
                   'destruction_total': float(sum(destruction, Decimal(0))),
                   'missing_stars': len(rows) - len(stars), 'missing_destruction': len(rows) - len(destruction)})


def account_measures(battles, after, before, mode):
    """Partition once, then measure both directions with the same selected window."""
    dated = [(search_date(battle['battle_at']), battle) for battle in battles]
    selected = [(at, battle) for at, battle in dated if after <= at < before
                and str(battle['payload'].get('battleType', '')).upper() in MODES[mode]]
    grouped = defaultdict(list)
    for _, battle in selected:
        payload = battle['payload']
        if type(payload.get('attack')) is bool:
            grouped[payload['attack']].append(payload)
    dates = [at for at, _ in selected]
    span = {'oldest': min(dates).isoformat() if dates else None,
            'newest': max(dates).isoformat() if dates else None}
    coverage = {'scope': 'matching_observed_battles', 'history_complete': False, 'order': 'newest_first',
                'chronology_complete': True, 'requested_after': after.isoformat(),
                'requested_before': before.isoformat(), 'before_exclusive': True,
                'available_rows': len(selected), 'matching_rows': len(selected), 'returned_rows': len(selected),
                'unknown_timestamp_rows': 0, 'available_range': span, 'selected_range': span, 'returned_range': span,
                'offset': 0, 'next_offset': None, 'selection_complete': True,
                'unknown_direction_rows': sum(type(b['payload'].get('attack')) is not bool for _, b in selected),
                'unknown_mode_rows': 0}
    measures = {direction: {'selection': ('ranked_' if mode == 'ranked_and_legend' else mode + '_') + direction,
                        **metrics(grouped[attack]), 'coverage': {
                            **coverage, 'excluded_rows': len(selected) - len(grouped[attack])}}
            for attack, direction in ((True, 'attacks'), (False, 'defenses'))}
    return [battle for _, battle in selected], measures


def cohort_measure(rows, direction, mode, after, before):
    available = [row['observed_' + direction] for row in rows if row.get('observed_' + direction) is not None]
    totals = {field: sum(Decimal(str(row[field])) for row in available) for field in TOTALS}
    totals = {key: float(value) if key == 'destruction_total' else int(value) for key, value in totals.items()}
    return {'selection': ('ranked_' if mode == 'ranked_and_legend' else mode + '_') + direction,
            'direction': direction, 'mode': mode, **ratios(totals),
            'coverage': {'scope': 'collected_ranked_history', 'history_complete': False,
                         'requested_after': after.isoformat(), 'requested_before': before.isoformat(),
                         'before_exclusive': True, 'requested_accounts': len(rows), 'available_accounts': len(available),
                         'unavailable_tags': [row['player_tag'] for row in rows
                                              if row.get('observed_' + direction) is None]}}
