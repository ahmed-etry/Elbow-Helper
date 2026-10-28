"""Lossless column tables for repeated nested writer records."""

from collections.abc import Mapping
import json


def writer_tables(value):
    """Share nested field names across records without dropping any values.

    Only uniformly shaped objects are flattened. A null, missing field, empty
    object or literal dot in a field name keeps that branch as an ordinary
    value, so absent evidence cannot turn into a measured zero or empty list.
    This encoding belongs to composition only; reports and grounding retain
    their original structures.
    """
    if isinstance(value, Mapping):
        if (set(value) == {'columns', 'rows'}
                and isinstance(value['columns'], list) and isinstance(value['rows'], list)
                and all(isinstance(key, str) for key in value['columns'])
                and len(set(value['columns'])) == len(value['columns'])
                and all(isinstance(row, list) and len(row) == len(value['columns'])
                        for row in value['rows'])):
            if not value['rows']:
                return {'columns': list(value['columns']), 'rows': []}
            return _table([dict(zip(value['columns'], row)) for row in value['rows']],
                          already_table=True)
        return {key: writer_tables(item) for key, item in value.items()}
    if isinstance(value, list):
        if (len(value) >= 10 and all(isinstance(row, Mapping) for row in value)
                and all(row.keys() == value[0].keys() for row in value)):
            return _table(value)
        return [writer_tables(item) for item in value]
    return value


def _table(records, *, already_table=False):
    if not records:
        return []
    columns = []
    cells = [[] for _ in records]

    def visit(keys, values):
        if (all(isinstance(item, Mapping) and item for item in values)
                and all(item.keys() == values[0].keys() for item in values)
                and all(isinstance(key, str) and '.' not in key for key in values[0])):
            for key in values[0]:
                visit((*keys, key), [item[key] for item in values])
        else:
            columns.append('.'.join(keys))
            for row, item in zip(cells, values):
                row.append(writer_tables(item))

    # A literal dot must remain a literal field name, rather than collide with
    # the path of another nested field. Such tables use the existing encoding.
    can_flatten = all(isinstance(key, str) and '.' not in key for key in records[0])
    if not can_flatten:
        return [{key: writer_tables(item) for key, item in record.items()} for record in records]
    for key in records[0]:
        values = [record[key] for record in records]
        visit((key,), values)
    shared = {}
    variable = []
    for index, column in enumerate(columns):
        first = cells[0][index]
        if all(_encode(row[index]) == _encode(first) for row in cells):
            shared[column] = first
        else:
            variable.append(index)
    table = {'columns': [columns[index] for index in variable],
             'rows': [[row[index] for index in variable] for row in cells]}
    if shared:
        table['shared'] = shared
    ordinary = [{key: writer_tables(item) for key, item in record.items()}
                for record in records]
    if already_table or _size(table) < _size(ordinary):
        return table
    return ordinary


def _size(value):
    return len(_encode(value))


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
