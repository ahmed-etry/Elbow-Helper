"""Shrink the text of each writer row while every row, listing and completeness flag stays.

A ratio keeps only its display value. War measure rows also state a field that
repeats another field, or is identical on every row, once, and share one shape
so the column tables can encode them. A field that says something is missing or
unconfirmed always stays on its row. Reports and grounding keep the original
rows; this is composition only.
"""

from collections.abc import Mapping

from elbow_helper.agent.reads.evidence import record_rows

WRITER_ROW_FAMILIES = frozenset({
    'read_war_attendance', 'read_member_cohort', 'read_member_accounts', 'read_recruitment_pipeline',
    'read_promotion_cases',
})
# Measure rows have one fixed set of fields, optional ones aside, so they can share one shape.
_SHAPED_FAMILIES = frozenset({'read_war_attendance', 'read_member_cohort'})
# `members` repeats owner identity for the rows.
_ROW_FIELDS = ('rows', 'members')
_RATIO = frozenset({'numerator', 'denominator', 'value', 'display'})
# (field, row field it repeats): omitted only when equal on every row that has it.
_REPEATS = (
    ('account_tags', 'player_tag'),
    ('attack_performance.wars', 'wars_observed'),
    ('attack_performance.stars_measured', 'attack_performance.observed_attacks'),
)


def _same(left, right):
    return type(left) is type(right) and left == right


def _lookup(row, path):
    value = row
    for part in path.split('.'):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _repeats_everywhere(rows, field, source):
    """True when `field` equals what it repeats on every row that carries it."""
    carrying = [row for row in rows if _lookup(row, field) is not None]
    if not carrying:
        return False
    for row in carrying:
        other = _lookup(row, source)
        value = _lookup(row, field)
        if other is None or not (value == [other] if field == 'account_tags' else _same(value, other)):
            return False
    return len(carrying) == len([row for row in rows if _lookup(row, source) is not None])


def _ratios(value):
    if isinstance(value, Mapping):
        if set(value) == _RATIO and isinstance(value['display'], str):
            return value['display']
        return {key: _ratios(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_ratios(item) for item in value]
    return value


def _without(row, path):
    head, _, rest = path.partition('.')
    if not rest:
        return {key: value for key, value in row.items() if key != head}
    if isinstance(row.get(head), Mapping):
        return {**row, head: _without(row[head], rest)}
    return row


def _shaped(rows):
    """State repeated and identical fields once and give every row the same fields."""
    repeated = {}
    for field, source in _REPEATS:
        if _repeats_everywhere(rows, field, source):
            repeated[field] = [source] if field == 'account_tags' else source
    for field in repeated:
        rows = [_without(row, field) for row in rows]
    constants = {}
    if len(rows) > 1:
        constants = {key: value for key, value in rows[0].items()
                     if all(key in row and _same(row[key], value) for row in rows[1:])}
        rows = [{key: value for key, value in row.items() if key not in constants} for row in rows]
    keys = list(dict.fromkeys(key for row in rows for key in row))
    absent = [key for key in keys if any(key not in row for row in rows)]
    if absent:
        # An optional field is null where the source had none, so rows share one shape.
        rows = [{key: row.get(key) for key in keys} for row in rows]
    return rows, repeated, constants, absent


def compact_writer_rows(facts, family):
    """Return `facts` with each row list rendered compactly; rows are never dropped."""
    result = dict(facts)
    notes = {}
    for field in _ROW_FIELDS:
        rows = record_rows(facts.get(field))
        if not isinstance(rows, list) or not rows or not all(isinstance(row, Mapping) for row in rows):
            continue
        rows = [_ratios(row) for row in rows]
        if family in _SHAPED_FAMILIES:
            rows, repeated, constants, absent = _shaped(rows)
            note = {name: value for name, value in (
                ('omitted_when_repeated', repeated), ('same_on_every_row', constants),
                ('null_when_absent', absent)) if value}
            if note:
                notes[field] = note
        result[field] = rows
    if notes:
        result['row_rendering'] = notes
    return result
