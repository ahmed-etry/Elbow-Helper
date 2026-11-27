"""Shape lookup results for the model: whole rows, each war list once, names for machine words, row pages."""
from __future__ import annotations

import json

from elbow_helper.agent.safety.access import READ_ACCESS_ROLE_SETS

from .names import access_labels, named

WAR_NUMBERS = "The n of each of the row's wars in war_aliases."
# query_bot_data's rows are the asker's own SQL output and stay as stored.
SQL_OUTPUT = ("rows", "columns")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(value):
    return len(_json(value).encode("utf-8"))


def shape(context, arguments, result, *, name=None):
    """What the model is shown for one lookup result."""
    if not isinstance(result, dict):
        return result
    skip = SQL_OUTPUT if name == "query_bot_data" else ()
    labels = access_labels(context.guild, READ_ACCESS_ROLE_SETS)
    if "error" in result:
        return named(result, labels, skip=skip)
    if result.get('kind') == 'family':
        result = {key: value for key, value in result.items()
                  if key not in ('next_cursor', 'next_offset')}
    return named(_war_lists_once(_roster_state(result)), labels, skip=skip)


def _roster_state(result):
    """A CWL roster row's status is the roster's state now, not its cycle's: name it so."""
    rosters = result.get('rosters')
    if not isinstance(rosters, list) or not any(
            isinstance(row, dict) and 'taking_signups' in row and 'status' in row for row in rosters):
        return result
    return {**result, 'rosters': [
        {('roster_status' if key == 'status' else key): value for key, value in row.items()}
        if isinstance(row, dict) and 'taking_signups' in row else row for row in rosters]}


def _war_lists_once(result):
    """Keep the war list in war_aliases only, and give each row its wars as numbers into it."""
    wars = result.get("war_aliases")
    if not isinstance(wars, list) or not wars or not all(
            isinstance(war, dict) and isinstance(war.get("war_id"), str) for war in wars):
        return result
    numbers, listed = {}, []
    for number, war in enumerate(wars, 1):
        aliases = war.get("aliases")
        for identifier in (war["war_id"], *(aliases if isinstance(aliases, list) else ())):
            numbers.setdefault(identifier, number)
        listed.append({"n": number, **{key: value for key, value in war.items()
                                        if key != "sources" and not (key == "aliases" and value == [war["war_id"]])}})
    result = {**result, "war_aliases": listed}
    every = set(range(1, len(wars) + 1))
    top = result.get("war_ids")
    if isinstance(top, list) and all(war in numbers for war in top) and {numbers[war] for war in top} == every:
        del result["war_ids"]
    rows = result.get("rows")
    if isinstance(rows, list):
        numbered = [_numbered_row(row, numbers) for row in rows]
        if numbered != rows:
            result["rows"] = numbered
            meanings = result.get("meanings")
            result["meanings"] = {**(meanings if isinstance(meanings, dict) else {}), "war_numbers": WAR_NUMBERS}
    return result


def _numbered_row(row, numbers):
    wars = row.get("war_ids") if isinstance(row, dict) else None
    if not isinstance(wars, list) or "war_numbers" in row or not all(war in numbers for war in wars):
        return row
    return {("war_numbers" if key == "war_ids" else key):
            (sorted({numbers[war] for war in wars}) if key == "war_ids" else value)
            for key, value in row.items()}


def row_pages(result, page_bytes, *, result_id=''):
    """Page details once, then every list by whole items, including wire metadata."""
    if _size(result) <= page_bytes:
        return [(None, result)]
    lists = [key for key, value in result.items() if isinstance(value, list) and value]
    lists.sort(key=lambda key: (key != 'rows', -_size(result[key])))
    details = {key: value for key, value in result.items() if key not in lists}
    # Every page contains at least one field or list item. This upper bound
    # reserves enough digits for the final page count before grouping items.
    maximum = len(details) + sum(len(result[key]) for key in lists)

    def info(number, total, label):
        more = number < total
        return {'page': number, 'total_pages': total, 'next_page': number + 1 if more else None,
                'items': label, 'content': 'This page is included below' + ('; use next_page to continue.' if more else '.')}

    def size(content, label):
        return _size({'result_page': {'result_id': result_id,
                      **info(maximum - 1, maximum, label)}, **content})

    # Each list keeps its own order, but can fill gaps left by earlier lists.
    # In particular a whole oversized row must not strand the other lists on
    # separate pages or leave a details-only first page.
    groups = []

    def label(ranges):
        return '; '.join(f'{key} {first}-{last} of {total}'
                         for key, (first, last, total) in ranges.items()) or 'details'

    def page_at(index):
        if index == len(groups):
            groups.append({'content': {}, 'ranges': {}, 'bytes': 2})
        return groups[index]

    def fits(group, extra, ranges):
        return size({}, label(ranges)) + group['bytes'] - 2 + extra <= page_bytes

    index = 0
    for key, value in details.items():
        extra = _size(key) + 1 + _size(value)
        while True:
            group = page_at(index)
            added = extra + bool(group['content'])
            if not group['content'] or fits(group, added, group['ranges']):
                group['content'][key] = value
                group['bytes'] += added
                break
            index += 1
    for key in lists:
        index = 0
        items = result[key]
        for number, item in enumerate(items, 1):
            item_bytes = _size(item)
            while True:
                group = page_at(index)
                content, ranges = group['content'], group['ranges']
                selected = {**ranges, key: (ranges.get(key, (number,))[0], number, len(items))}
                extra = item_bytes + (1 if key in content else _size(key) + 3 + bool(content))
                if not content or fits(group, extra, selected):
                    content.setdefault(key, []).append(item)
                    group['ranges'] = selected
                    group['bytes'] += extra
                    break
                index += 1
    return [(info(number, len(groups), label(group['ranges'])), group['content'])
            for number, group in enumerate(groups, 1)]
