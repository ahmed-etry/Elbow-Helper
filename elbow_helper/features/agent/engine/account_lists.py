"""Write account lists in assessment answers from authorized rows, not model-copied facts.

The model chooses which accounts belong in a list and supplies only its own judgement for
each one. Names, Town Hall levels and current clans come from the rows this request read.
"""
from collections.abc import Mapping
import re

import discord

from elbow_helper.configuration.clans import CLANS
from ..families.model_view import record_rows

ACCOUNT_LISTS_SCHEMA = {'type': 'array', 'maxItems': 12, 'items': {
    'type': 'object', 'properties': {
        'id': {'type': 'string', 'pattern': '^[a-z0-9_-]{1,32}$'},
        'title': {'type': 'string', 'minLength': 0, 'maxLength': 120},
        'current_clan': {'type': 'string', 'enum': list(CLANS)},
        'accounts': {'type': 'array', 'minItems': 1, 'maxItems': 300, 'items': {
            'type': 'object', 'properties': {
                'account': {'type': 'string', 'minLength': 1, 'maxLength': 60},
                'estimate': {'type': 'string', 'minLength': 0, 'maxLength': 40},
                'note': {'type': 'string', 'minLength': 0, 'maxLength': 160}},
            'required': ['account'], 'additionalProperties': False}}},
    'required': ['id', 'accounts'], 'additionalProperties': False}}

ACCOUNT_LISTS_INSTRUCTION = """Account lists: when the answer lists accounts from the evidence, put them
in account_lists instead of writing their rows yourself. Refer to each account by its player_tag
from the evidence, since names can repeat, and give only your estimate or a short note; code
writes its name, Town Hall and current clan from the data. Put
[[accounts:ID]] in facts or recommendation where each list belongs. Set current_clan when a list
says its accounts are currently in one clan. Do not repeat listed rows in prose."""


def _current_clans(result):
    """Clans whose rows in this result describe current in-game membership."""
    clans = result.get('clans')
    if not isinstance(clans, list):
        return set()
    return {clan.get('clan_code') for clan in clans
            if isinstance(clan, Mapping) and clan.get('basis') == 'current_in_game'}


def account_index(evidence, limits=None):
    """Map player tags to the facts code can state about them.

    Current clan comes only from current in-game roster or roster-cohort rows; historical
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
                current = (account.get('basis', row.get('basis')) == 'current_in_game'
                           or account.get('clan_code') in cohort_current)
                entry = index.setdefault(account['player_tag'], {})
                name = account.get('player_name')
                level = account.get('townHallLevel', account.get('townhall'))
                if name and (current or 'name' not in entry):
                    entry['name'] = name
                if type(level) is int and (current or 'town_hall' not in entry):
                    entry['town_hall'] = level
                if current and account.get('clan_code'):
                    entry['current_clan'] = account['clan_code']
    for account in (limits or {}).get('unverified_selection', ()):
        if account.get('player_tag') in index:
            index[account['player_tag']]['unverified_owner'] = True
    return index


def _text(value):
    """Account names are member-controlled; they cannot format or mention."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _plain(value):
    return discord.utils.escape_mentions(str(value))


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


def render_list(declaration, index):
    lines = []
    for item in declaration['accounts']:
        if item.get('player_tag') is None:
            # An unresolved name stays visible and flagged rather than sinking the answer.
            parts = [_text(item['account'].strip())]
            if item.get('estimate', '').strip():
                parts.append(_plain(item['estimate'].strip()))
            lines.append(' — '.join(parts) + ' (' + item['unresolved'] + ')')
            continue
        facts = index[item['player_tag']]
        parts = [_text(facts.get('name') or item['player_tag'])]
        if facts.get('town_hall'):
            parts.append('TH' + str(facts['town_hall']))
        if item.get('estimate', '').strip():
            parts.append(_plain(item['estimate'].strip()))
        line = ' — '.join(parts)
        flags = [_plain(item['note'].strip())] if item.get('note', '').strip() else []
        expected = declaration.get('current_clan')
        if expected and facts.get('current_clan') != expected:
            flags.append('currently in ' + facts['current_clan'] if facts.get('current_clan')
                         else 'current clan unknown')
        if facts.get('unverified_owner'):
            flags.append('owner unverified')
        lines.append(line + (' (' + '; '.join(flags) + ')' if flags else ''))
    # Code states the count; drop one the title already carries.
    title = re.sub(r'\s*(?:\(\d+\)|[—–-]\s*\d+)\s*$', '', declaration.get('title', '').strip())
    header = [_plain(title) + ' (' + str(len(lines)) + ')'] if title else []
    return '\n'.join(header + lines)


def insert_account_lists(parts, declarations, index):
    """Replace [[accounts:ID]] markers; a list without a marker follows the first part."""
    identities = [declaration['id'] for declaration in declarations or ()]
    if len(set(identities)) != len(identities):
        raise ValueError('Use distinct account list IDs.')
    resolved = []
    for declaration in declarations or ():
        accounts, seen = [], set()
        for item in declaration['accounts']:
            tag, reason = resolve_account(item['account'], index, declaration.get('current_clan'))
            key = tag or item['account'].strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            accounts.append({**item, 'player_tag': tag, **({'unresolved': reason} if reason else {})})
        resolved.append({**declaration, 'accounts': accounts})
    declarations = resolved
    parts = list(parts)
    for declaration in declarations:
        marker = '[[accounts:' + declaration['id'] + ']]'
        rendered = render_list(declaration, index)
        found = [position for position, part in enumerate(parts) if marker in part]
        if sum(part.count(marker) for part in parts) > 1:
            raise ValueError('Place each account list once: ' + declaration['id'])
        if found:
            parts[found[0]] = parts[found[0]].replace(marker, rendered)
        else:
            parts[0] = '\n\n'.join(filter(None, (parts[0].strip(), rendered)))
    if any('[[accounts:' in part for part in parts):
        raise ValueError('Use a declared account list ID in each [[accounts:ID]] marker.')
    return parts
