"""Resolve lookup labels against game accounts without expanding their owners."""

import asyncio
import sqlite3
import unicodedata

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from ..access import require_evidence_access
from ..datasets.clash import read_clash


def normalized_name(value):
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold()
                   if char.isalnum())


def edit_distance(left, right):
    """Small spelling edits, including a transposed pair of characters."""
    previous, older = list(range(len(right) + 1)), None
    for index, char in enumerate(left, 1):
        current = [index]
        for slot, other in enumerate(right, 1):
            value = min(current[-1] + 1, previous[slot] + 1,
                        previous[slot - 1] + (char != other))
            if (older is not None and slot > 1 and char == right[slot - 2]
                    and left[index - 2] == other):
                value = min(value, older[slot - 2] + 1)
            current.append(value)
        older, previous = previous, current
    return previous[-1]


def matching_accounts(query, accounts):
    """Exact normalized names win; multiple plausible tags remain ambiguous."""
    if query.strip().startswith('#'):
        tag = normalize_player_tag(query)
        return [accounts[tag]] if tag in accounts else []
    target = normalized_name(query)
    if not target:
        return []
    exact, fuzzy = [], []
    for account in accounts.values():
        aliases = [normalized_name(name) for name in account['names']]
        if target in aliases:
            exact.append(account)
        elif len(target) >= 4 and any(
                min(len(target), len(name)) / max(len(target), len(name)) >= 0.65
                and abs(len(target) - len(name)) <= min(3, max(1, max(len(target), len(name)) // 3))
                and edit_distance(target, name) <= min(3, max(1, max(len(target), len(name)) // 3))
                for name in aliases if name):
            fuzzy.append(account)
    return sorted(exact or fuzzy, key=lambda row: row['player_tag'])


async def resolve_account_names(context, queries):
    await require_evidence_access(context)
    accounts, missing = {}, []

    def add(tag, name, source):
        tag = normalize_player_tag(tag)
        if tag is None or not isinstance(name, str) or not name.strip():
            return
        account = accounts.setdefault(tag, {'player_tag': tag, 'player_name': name,
                                            'names': [], 'sources': []})
        if name not in account['names']:
            account['names'].append(name)
        if source not in account['sources']:
            account['sources'].append(source)
        if source == 'current_family_roster':
            account['player_name'] = name

    async def stored(source, reader):
        if reader is None:
            missing.append({'source': source, 'cause': 'missing_setup'})
            return
        try:
            rows = await asyncio.to_thread(reader)
        except (OSError, sqlite3.Error, TimeoutError):
            missing.append({'source': source, 'cause': 'source_unavailable'})
            return
        if isinstance(rows, dict):
            rows = rows.values()
        for row in rows:
            add(row.get('player_tag'), row.get('player_name_last_seen') or row.get('player_name'), source)

    await stored('bot_account_links', context.account_links.get_all_links if context.account_links else None)
    await stored('stored_account_directory', context.cwl_queries.account_names if context.cwl_queries else None)
    live = await read_clash(context, {'kind': 'clan', 'tags': [clan.tag for clan in CLANS.values()],
                                     'member_scope': 'current', 'detail': 'full', 'limit': 50})
    by_tag = {item['tag']: item for item in live.get('items', [])}
    for code, clan in CLANS.items():
        item = by_tag.get(clan.tag, {})
        if item.get('status') != 'ok':
            missing.append({'source': 'current_family_roster', 'clan_code': code,
                            'cause': item.get('status') or live.get('cause', 'source_unavailable')})
        else:
            for row in item.get('data', {}).get('memberList', []):
                add(row.get('tag'), row.get('name'), 'current_family_roster')
    await require_evidence_access(context)
    resolutions, selected = [], {}
    for query in queries:
        candidates = matching_accounts(query, accounts)
        status = ('resolved' if len(candidates) == 1 else 'ambiguous' if candidates
                  else 'unknown' if missing else 'not_found')
        resolutions.append({'query': query, 'status': status, 'candidates': candidates})
        if status == 'resolved':
            selected[candidates[0]['player_tag']] = candidates[0]
    return {'accounts': selected, 'name_resolutions': resolutions, 'missing_sources': missing,
            'selection_complete': all(row['status'] == 'resolved' for row in resolutions)}
