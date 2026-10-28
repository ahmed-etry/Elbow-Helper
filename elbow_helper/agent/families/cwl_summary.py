"""Compute selected-cohort ratios and comparable score extrema before paging."""
from collections import defaultdict
import json
import math

CWL_ANALYSIS_FIELDS = ('selected_group_metrics', 'score_comparisons', 'analysis_scope')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if numerator is not None and denominator else None}


def cwl_analysis(rows, metric, *, scopes=()):
    """Summaries describe the selected rows, never a broader unobserved population.

    Compute before top_n/transport paging, after explicit account selection.
    Scoring profiles and periods are separate comparison populations. Unknown
    fields stay unknown; a mean of account scores is not stars per attack.
    """
    if metric not in ('performance', 'ass'):
        return {}
    groups, scores = defaultdict(list), {}
    context = {scope.get('clan_code'): scope for scope in scopes}
    for row in rows:
        if metric == 'performance':
            groups[(row.get('season'), row.get('clan_code'))].append(row)
            scope = {'season': row.get('season'), 'profile_key': row.get('profile_key')}
            field = 'score'
        else:
            source = context.get(row.get('clan_code'),
                next(iter(context.values())) if len(context) == 1 else {})
            scope = {key: source.get(key) for key in (
                'seasons', 'completed_wars', 'profile_key', 'difficulty_weight', 'missed_mode')}
            field = 'ass_score'
        if (not scope.get('profile_key') or not number(row.get(field))
                or not (scope.get('season') or scope.get('seasons'))
                or metric == 'ass' and not scope.get('completed_wars')):
            continue
        key = json.dumps(scope, sort_keys=True)
        group = scores.setdefault(key, {'scope': scope, 'score_field': field,
            'scored_accounts': 0, 'highest_score': row[field], 'highest_accounts': []})
        group['scored_accounts'] += 1
        if row[field] > group['highest_score']:
            group.update(highest_score=row[field], highest_accounts=[])
        if row[field] == group['highest_score']:
            group['highest_accounts'].append({
                'clan_code': row.get('clan_code', source.get('clan_code') if metric == 'ass' else None),
                'player_tag': row['player_tag'],
                **({'player_name': row['player_name']} if row.get('player_name') else {})})
    summaries = []
    for (season, clan), selected in sorted(groups.items(), key=lambda item: json.dumps(item[0])):
        def total(field):
            values = [row.get(field) for row in selected]
            return sum(values) if all(number(value) and value >= 0 for value in values) else None
        stars, attacks, expected = total('stars'), total('attacks'), total('attacks_expected')
        observed = [row['score'] for row in selected if number(row.get('score'))]
        summaries.append({'season': season, 'clan_code': clan, 'selected_accounts': len(selected),
            'stars': stars, 'attacks': attacks, 'attacks_expected': expected,
            'stars_per_attack': ratio(stars, attacks),
            'participation': ratio(attacks, expected),
            'mean_observed_account_score': ratio(sum(observed), len(observed)),
            'accounts_without_score': len(selected) - len(observed)})
    return {'selected_group_metrics': summaries, 'score_comparisons': list(scores.values()),
            'analysis_scope': 'All explicitly selected accounts before top_n and transport paging; '
                'score comparisons are only within the same stated period and scoring profile. '
                'Ratios use summed numerators and denominators, not averages of account averages.'}
