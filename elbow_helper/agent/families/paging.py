"""Lossless family continuation at a smaller remaining evidence budget."""
import json

from .cursor import family_cursor


def bound_family_page(payload, limit):
    if payload.get('kind') != 'family' or not payload.get('report_id'):
        return None
    records = payload.get('rows')
    table = isinstance(records, dict) and set(records) == {'columns', 'rows'}
    rows = records['rows'] if table else records
    if not isinstance(rows, list):
        return None
    offset = payload.get('offset', 0)

    def page(count):
        result = {**payload, 'rows': {**records, 'rows': rows[:count]} if table else rows[:count],
                  'returned_rows': count}
        following = offset + count if count < len(rows) else payload.get('next_offset')
        result['next_offset'] = following
        result['next_cursor'] = (family_cursor(payload['report_id'], payload.get('requester_id'),
            payload.get('selection', {}), following) if following is not None else None)
        return json.dumps(result, ensure_ascii=False, separators=(',', ':'))

    low, high, fitted = 1, len(rows), None
    while low <= high:
        middle = (low + high) // 2
        candidate = page(middle)
        if len(candidate) <= limit:
            fitted = candidate
            low = middle + 1
        else:
            high = middle - 1
    if fitted is not None:
        return fitted
    refusal = json.dumps({'error': 'The remaining evidence budget cannot fit a complete report row.',
        'cause': 'capacity', 'report_id': payload['report_id'], 'offset': offset}, separators=(',', ':'))
    return refusal if len(refusal) <= limit else '{}'
