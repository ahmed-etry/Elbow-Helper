"""Shape account activity records for agent lookups."""


def pick(value, fields):
    return {key: value[key] for key in fields if key in value}


def ranked_view(row):
    # A fixed schema lets the transport encode large selections once as a
    # column table, including accounts whose names or ranking are unknown.
    fields = ('player_tag', 'member_id', 'member_name', 'player_name', 'status', 'selection_rank',
        'attacks_used', 'attacks_available', 'counts_complete', 'ranked_league', 'watch_status',
        'watched_from', 'historical_counts_complete')
    result = {field: row.get(field) for field in fields}
    result.update(pick(row, ('observed_at', 'ranking_observed_at')))
    for field in ('observed_attacks', 'observed_defenses'):
        metric = row.get(field)
        if isinstance(metric, dict):
            result[field] = pick(metric, ('selection', 'battles', 'stars_total', 'triples',
                'missing_stars', 'missing_destruction', 'average_stars',
                'average_destruction_percent', 'triple_rate_percent'))
            result[field]['observed_range'] = metric.get('coverage', {}).get('available_range')
        else:
            result[field] = metric
    return result
