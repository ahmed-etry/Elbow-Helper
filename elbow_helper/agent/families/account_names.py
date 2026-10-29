"""Join named Discord members and family game accounts without choosing a side."""

import asyncio
import sqlite3
import unicodedata

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.guild import TEST_ACCOUNT_IDS
from elbow_helper.domain.player_tags import normalize_player_tag

from ..access import require_evidence_access
from ..datasets.clash import read_clash
from ..research.member_identity import matching_members, matches_discord_identity


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
    """Prefer exact names, then word prefixes; allow small edits per word."""
    if query.strip().startswith('#'):
        tag = normalize_player_tag(query)
        return [accounts[tag]] if tag in accounts else []
    target = normalized_name(query)
    if not target:
        return []
    exact, prefix, fuzzy = [], [], []
    for account in accounts.values():
        aliases = [normalized_name(name) for name in account['names']]
        if target in aliases:
            exact.append(account)
        elif any(word_prefix(query, name) for name in account['names']):
            prefix.append(account)
        elif any(small_spelling_edit(query, name) for name in account['names']):
            fuzzy.append(account)
    return sorted(exact or prefix or fuzzy, key=lambda row: row['player_tag'])


def name_segments(value):
    return ''.join(char if char.isalnum() else ' '
                   for char in unicodedata.normalize('NFKC', value).casefold()).split()


def word_prefix(query, name):
    left, right = name_segments(query), name_segments(name)
    return bool(left) and len(left) == len(right) and all(
        other.startswith(part) for part, other in zip(left, right))


def small_spelling_edit(query, name):
    # Formatting-only changes resolve through normalized exact matching above.
    # For spelling changes, align every word and budget its edits independently;
    # a long word's allowance must not absorb another word or an initial.
    left, right = name_segments(query), name_segments(name)
    return bool(left) and len(left) == len(right) and all(
        edit_distance(part, other) <= (1 if max(len(part), len(other)) <= 5 else 2)
        for part, other in zip(left, right))


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
    resolutions = [{'query': query, 'candidates': matching_accounts(query, accounts)} for query in queries]
    # Join every candidate before deciding whether matching accounts identify
    # one owner. Unowned candidates must still require clarification.
    candidates = {row['player_tag']: row for resolution in resolutions
                  for row in resolution['candidates']}
    links = (await asyncio.to_thread(context.account_links.get_links_by_tags, sorted(candidates))
             if candidates and context.account_links else {})
    await require_evidence_access(context)
    for tag, candidate in candidates.items():
        link = links.get(tag)
        identifier = link['discord_user_id'] if link else None
        member = context.guild.get_member(identifier) if identifier else None
        candidate.update(member_id=identifier, member_name=member.display_name if member else None,
                         membership_status=('links_unavailable' if context.account_links is None else
                             'unlinked' if identifier is None else 'present' if member else 'not_in_guild'))
    selected = {}
    for resolution in resolutions:
        matches = resolution['candidates']
        owners = {row['member_id'] for row in matches}
        same_owner = len(owners) == 1 and None not in owners
        status = ('resolved' if len(matches) == 1 or same_owner else 'ambiguous' if matches
                  else 'unknown' if missing else 'not_found')
        resolution['status'] = status
        if status == 'resolved':
            selected.update((row['player_tag'], row) for row in matches)
    return {'accounts': selected, 'name_resolutions': resolutions, 'missing_sources': missing,
            'selection_complete': all(row['status'] == 'resolved' for row in resolutions)}


async def resolve_names(context, queries):
    """Use the same two-sided lookup for singular and plural name selectors."""
    await require_evidence_access(context)
    # Account-name exactness must not suppress somebody else's nickname part.
    member_matches = [await matching_members(context, query.strip(), prefer_exact=False) for query in queries]
    members_complete = getattr(context.guild, 'chunked', True) is not False
    result = await resolve_account_names(context, queries)
    if not members_complete:
        result['missing_sources'].append({'source': 'discord_members', 'cause': 'incomplete'})
    members, sources_by_query = {}, []
    for matches, resolution in zip(member_matches, result['name_resolutions']):
        sources = {member.id: ['discord_or_linked_account_name'] for member in matches}
        members.update((member.id, member) for member in matches)
        for candidate in resolution['candidates']:
            identifier = candidate['member_id']
            member = context.guild.get_member(identifier) if identifier else None
            if (member is None or member.id != identifier or identifier in TEST_ACCOUNT_IDS
                    or getattr(member, 'bot', False)):
                continue
            members[identifier] = member
            member_sources = sources.setdefault(identifier, [])
            if 'bot_account_link' not in member_sources:
                member_sources.append('bot_account_link')
        sources_by_query.append(sources)
    links = (await asyncio.to_thread(context.account_links.get_links_for_members, sorted(members))
             if members and context.account_links else {})
    await require_evidence_access(context)
    selected, selected_members = {}, set()
    for resolution, sources in zip(result['name_resolutions'], sources_by_query):
        candidates = resolution['candidates']
        account_status = resolution['status']
        # An exact account match must never hide a Discord name belonging to
        # somebody else, including when the account has no current guild owner.
        collision = bool(candidates and sources and any(
            candidate['member_id'] != identifier
            for candidate in candidates for identifier in sources))
        ambiguous = len(sources) > 1 or account_status == 'ambiguous' or collision
        status = ('ambiguous' if ambiguous else 'unknown' if not members_complete else
                  'resolved' if candidates or sources else account_status)
        resolution.update(status=status, account_status=account_status,
            member_status=('ambiguous' if ambiguous and sources else 'unknown' if not members_complete else
                           'resolved' if sources else 'not_found'),
            matching_members=[{
                'member_id': identifier, 'member_name': members[identifier].display_name,
                'username': getattr(members[identifier], 'name', None),
                'nickname': getattr(members[identifier], 'nick', None),
                'match_sources': sources[identifier],
                'accounts': [{'player_tag': row['player_tag'],
                              'player_name': row.get('player_name_last_seen')}
                             for row in links.get(identifier, [])],
                'account_links_status': 'available' if context.account_links else 'links_unavailable',
            } for identifier in sorted(sources)])
        if status != 'resolved':
            continue
        selected_members.update(sources)
        if candidates:
            selected.update((row['player_tag'], row) for row in candidates)
        # A Discord identity match selects all linked accounts even when some
        # account names also match. Account-only matches keep their own tags.
        for identifier in sources:
            if candidates and not matches_discord_identity(members[identifier], resolution['query'].strip()):
                continue
            for row in links.get(identifier, []):
                selected.setdefault(row['player_tag'], {
                    'player_tag': row['player_tag'], 'player_name': row.get('player_name_last_seen')})
    return {'accounts': selected, 'member_ids': sorted(selected_members),
            'name_resolutions': result['name_resolutions'], 'missing_sources': result['missing_sources'],
            'selection_complete': all(row['status'] == 'resolved' for row in result['name_resolutions'])}
