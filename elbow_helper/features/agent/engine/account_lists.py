"""Write account lists in assessment answers from authorized rows, not model-copied facts.

The model chooses which accounts belong in a list and supplies only its own judgement for
each one. Names, Town Hall levels and current clans come from the rows this request read.
"""
from collections.abc import Mapping
import re

import discord

from elbow_helper.configuration.clans import CLANS
from ..families.model_view import record_rows

FACT_FIELDS = ('name', 'town_hall', 'current_clan', 'player_tag', 'flags')
COLUMN_SCHEMA = {'anyOf': [
    {'type': 'object', 'properties': {
        'heading': {'type': 'string', 'minLength': 1, 'maxLength': 120},
        'fact': {'type': 'string', 'enum': list(FACT_FIELDS)}},
     'required': ['heading', 'fact'], 'additionalProperties': False},
    {'type': 'object', 'properties': {
        'heading': {'type': 'string', 'minLength': 1, 'maxLength': 120},
        'value': {'type': 'string', 'pattern': '^[a-z0-9_-]{1,32}$'}},
     'required': ['heading', 'value'], 'additionalProperties': False}]}

ACCOUNT_LISTS_SCHEMA = {'type': 'array', 'maxItems': 12, 'items': {
    'type': 'object', 'properties': {
        'id': {'type': 'string', 'pattern': '^[a-z0-9_-]{1,32}$'},
        'title': {'type': 'string', 'minLength': 0, 'maxLength': 120},
        'current_clan': {'type': 'string', 'enum': list(CLANS)},
        'columns': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': COLUMN_SCHEMA},
        'accounts': {'type': 'array', 'minItems': 1, 'maxItems': 300, 'items': {
            'type': 'object', 'properties': {
                'account': {'type': 'string', 'minLength': 1, 'maxLength': 60},
                'values': {'type': 'object', 'additionalProperties': {
                    'type': 'string', 'minLength': 0, 'maxLength': 160}}},
            'required': ['account'], 'additionalProperties': False}}},
    'required': ['id', 'columns', 'accounts'], 'additionalProperties': False}}

ACCOUNT_LISTS_INSTRUCTION = """Use account_lists for account selections, referencing evidence player_tags.
Choose column headings/order for the request: fact selects verified data; value selects a
judgement key in each account's values. Never put verified facts in values. Include fact=flags
when qualifications matter. Put each [[accounts:ID]] marker on its own line, without repeating
its title or rows in prose. Set current_clan only for lists claiming current membership."""


def _current_clans(result):
    """Clans whose rows in this result describe current in-game membership."""
    clans = result.get('clans')
    if not isinstance(clans, list):
        return set()
    return {clan.get('clan_code') for clan in clans
            if isinstance(clan, Mapping) and clan.get('basis') == 'current_in_game'}


def account_index(evidence, limits=None):
    """Map player tags to the facts code can state about them.

    Live profiles and current in-game rosters supply current membership; historical
    war, CWL or ranking rows supply a name or Town Hall when nothing current does.
    """
    index = {}
    for result in evidence.values():
        if not isinstance(result, Mapping) or result.get('error'):
            continue
        rows = record_rows(result.get('rows'))
        if not isinstance(rows, list):
            continue
        cohort_current = (_current_clans(result)
                          if result.get('basis') == 'clan_rosters' else set())
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            accounts = row.get('accounts') if isinstance(row.get('accounts'), list) else [row]
            for account in accounts:
                if not isinstance(account, Mapping) or not account.get('player_tag'):
                    continue
                profile = account.get('profile_status') == 'ok'
                current = (profile or account.get('basis', row.get('basis')) == 'current_in_game'
                           or account.get('clan_code') in cohort_current)
                entry = index.setdefault(account['player_tag'], {})
                name = account.get('player_name')
                level = account.get('townHallLevel', account.get('townhall'))
                if name and (current or 'name' not in entry):
                    entry['name'] = name
                if type(level) is int and (current or 'town_hall' not in entry):
                    entry['town_hall'] = level
                if profile:
                    clan = account.get('clan')
                    code = (next((code for code, configured in CLANS.items()
                                  if configured.tag == clan.get('tag')),
                                 clan.get('name') or clan.get('tag'))
                            if isinstance(clan, Mapping) else None)
                    if code:
                        entry['current_clan'] = code
                    else:
                        entry.pop('current_clan', None)
                elif current and account.get('clan_code'):
                    entry['current_clan'] = account['clan_code']
    for account in (limits or {}).get('unverified_selection', ()):
        if account.get('player_tag') in index:
            index[account['player_tag']]['unverified_owner'] = True
    return index


def _text(value):
    """Account names are member-controlled; they cannot format or mention."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


AMBIGUOUS = 'several accounts share this name'
UNVERIFIED = "couldn't verify this account"


def resolve_account(reference, index, clan=None):
    """Return (player tag, None) for a tag or a uniquely named account, else (None, reason).

    A name several accounts share is settled by the clan the list says they are in.
    """
    reference = reference.strip()
    if reference in index:
        return reference, None
    for exact in (True, False):
        matches = [tag for tag, facts in index.items() if facts.get('name') is not None and (
            facts['name'] == reference if exact else facts['name'].casefold() == reference.casefold())]
        if len(matches) > 1 and clan:
            matches = [tag for tag in matches if index[tag].get('current_clan') == clan] or matches
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, AMBIGUOUS
    return None, UNVERIFIED


def account_values(declaration, item, index):
    """Materialize the chosen cells without accepting model copies of facts."""
    facts = index.get(item.get('player_tag'), {})
    flags = '; '.join(account_flags(declaration, item, index))
    verified = {**facts, 'name': facts.get('name') or item.get('player_tag') or item['account'].strip(),
                'player_tag': item.get('player_tag') or '', 'flags': flags}
    cells = [verified.get(column['fact'], '') if 'fact' in column
             else item.get('values', {}).get(column['value'], '').strip()
             for column in declaration['columns']]
    if flags and not any(column.get('fact') == 'flags' for column in declaration['columns']):
        cells[-1] = str(cells[-1]) + ' (' + flags + ')'
    return tuple(cells)


def list_title(declaration):
    # Code states the count; drop one the title already carries.
    return re.sub(r'\s*(?:\(\d+\)|[—–-]\s*\d+)\s*$', '', declaration.get('title', '').strip())


def render_list(declaration, index):
    lines = [' — '.join(_text(value) if column.get('fact') in ('name', 'current_clan', 'flags')
                        else discord.utils.escape_mentions(str(value))
                        for column, value in zip(declaration['columns'], account_values(declaration, item, index))
                        if str(value).strip()) for item in declaration['accounts']]
    title = list_title(declaration)
    header = ['**' + _text(title) + ' (' + str(len(lines)) + ')**'] if title else []
    return '\n'.join(header + lines)


def account_flags(declaration, item, index):
    """Share the existing row qualifications with workbook delivery."""
    if item.get('player_tag') is None:
        return [item['unresolved']]
    facts = index[item['player_tag']]
    flags = []
    expected = declaration.get('current_clan')
    if expected and facts.get('current_clan') != expected:
        flags.append('currently in ' + facts['current_clan'] if facts.get('current_clan')
                     else 'current clan unknown')
    if facts.get('unverified_owner'):
        flags.append('owner unverified')
    return flags


def resolve_account_lists(declarations, index):
    """Resolve and deduplicate once for either text or workbook delivery."""
    identities = [declaration['id'] for declaration in declarations or ()]
    if len(set(identities)) != len(identities):
        raise ValueError('Use distinct account list IDs.')
    resolved = []
    for declaration in declarations or ():
        columns = declaration['columns']
        keys = [column['value'] for column in columns if 'value' in column]
        if (len(set(column['heading'].casefold() for column in columns)) != len(columns)
                or len(set(keys)) != len(keys) or set(keys) & set(FACT_FIELDS)):
            raise ValueError('Use distinct headings and judgement keys, separate from account facts.')
        accounts, seen = [], set()
        for item in declaration['accounts']:
            if set(item.get('values', {})) - set(keys):
                raise ValueError('Account values must belong to declared judgement columns.')
            tag, reason = resolve_account(item['account'], index, declaration.get('current_clan'))
            key = tag or item['account'].strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            accounts.append({**item, 'player_tag': tag, **({'unresolved': reason} if reason else {})})
        resolved.append({**declaration, 'accounts': accounts})
    return resolved


def insert_account_lists(parts, declarations, index):
    """Replace [[accounts:ID]] markers; a list without a marker follows the first part."""
    return insert_resolved_lists(parts, resolve_account_lists(declarations, index), index)


def insert_resolved_lists(parts, declarations, index, *, delivered=False):
    """Give inline lists block boundaries; prepared workbooks leave no marker text."""
    parts = list(parts)
    placements = [[] for part in parts]
    unmarked = []
    remaining = list(parts)
    for declaration in declarations:
        marker = '[[accounts:' + declaration['id'] + ']]'
        found = [position for position, part in enumerate(parts) if marker in part]
        if sum(part.count(marker) for part in parts) > 1:
            raise ValueError('Place each account list once: ' + declaration['id'])
        if found:
            position = found[0]
            placements[position].append((parts[position].index(marker), marker, declaration))
            remaining[position] = remaining[position].replace(marker, '')
        elif not delivered:
            unmarked.append(declaration)
    if any('[[accounts:' in part for part in remaining):
        raise ValueError('Use a declared account list ID in each [[accounts:ID]] marker.')
    for position, locations in enumerate(placements):
        if not locations:
            continue
        original, cursor, chunks = parts[position], 0, []
        for offset, marker, declaration in sorted(locations):
            before = original[cursor:offset]
            if not delivered:
                title = list_title(declaration)
                if title:
                    # Inspect only the original model prose, before inserting
                    # any source account names or previously rendered rows.
                    repeated = r'(?<!\w)(?:\*\*)?' + re.escape(title) + r'(?:\s*\(\d+\))?(?:\*\*)?\s*[:：—–-]?\s*$'
                    before = re.sub(repeated, '', before, flags=re.IGNORECASE)
            chunks.append(before)
            if not delivered:
                chunks.append(render_list(declaration, index))
            cursor = offset + len(marker)
        chunks.append(original[cursor:])
        parts[position] = (''.join(chunks) if delivered else
                           '\n\n'.join(chunk.strip() for chunk in chunks if chunk.strip()))
    for declaration in unmarked:
        parts[0] = '\n\n'.join(filter(None, (parts[0].strip(), render_list(declaration, index))))
    return parts
