"""Lossless family continuation at a smaller remaining evidence budget."""
import json

from elbow_helper.agent.reads.cursors import family_cursor


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
    # No row fits this transport page, but the lookup and its saved report succeeded.
    # Keep its locator when possible; the writer restores rows from the original read.
    empty = {key: payload[key] for key in ('kind', 'family', 'report_id', 'total_rows',
             'complete_snapshot', 'view', 'limit') if key in payload}
    empty.update(rows=[], returned_rows=0, offset=offset,
                 next_offset=offset if rows else payload.get('next_offset'))
    content = json.dumps(empty, ensure_ascii=False, separators=(',', ':'))
    return content if len(content) <= limit else '{}'
