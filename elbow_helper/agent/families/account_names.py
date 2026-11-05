"""Resolve names on the planner's chosen side, then join through account links."""

import asyncio
import sqlite3
from dataclasses import replace

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.guild import TEST_ACCOUNT_IDS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.domain.identity import (matching_accounts, normalized_name, edit_distance,
                                          name_segments, word_prefix, small_spelling_edit)

from ..access import require_evidence_access
from ..datasets.clash import read_clash
from ..research.member_identity import matching_members, matches_discord_identity
from .. import record_identity


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
    for tag, names in (await record_identity.aliases(context, queries)).items():
        for name in names:
            add(tag, name, 'family_record')
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


async def resolve_names(context, queries, look_in=None):
    """Search the requested identity source; only misses use the other source."""
    if look_in not in ('game', 'discord'):
        return {'error': 'look_in is required for name lookups; choose game or discord.',
                'cause': 'invalid_arguments', 'invalid_fields': ['look_in']}
    await require_evidence_access(context)
    resolutions, missing, members = [None] * len(queries), [], {}

    async def search(side, indices):
        if side == 'game':
            result = await resolve_account_names(context, [queries[index] for index in indices])
            for source in result['missing_sources']:
                if source not in missing:
                    missing.append(source)
            for index, resolution in zip(indices, result['name_resolutions']):
                resolution['found_in'] = 'game' if resolution['candidates'] else None
                resolutions[index] = resolution
            return
        # matching_members also supports linked aliases for other callers. Here
        # only Discord identities may match; links are joined after selection.
        discord_context = replace(context, account_links=None)
        for index in indices:
            query = queries[index].strip()
            matches = [member for member in await matching_members(
                discord_context, query, prefer_exact=False)
                if matches_discord_identity(member, query)]
            complete = getattr(context.guild, 'chunked', True) is not False
            if not complete and {'source': 'discord_members', 'cause': 'incomplete'} not in missing:
                missing.append({'source': 'discord_members', 'cause': 'incomplete'})
            members.update((member.id, member) for member in matches)
            resolutions[index] = {'query': queries[index],
                'status': ('unknown' if not complete else 'ambiguous' if len(matches) > 1
                           else 'resolved' if matches else 'not_found'),
                'found_in': 'discord' if matches else None, 'candidates': [],
                'matching_members': [{'member_id': member.id, 'member_name': member.display_name,
                    'username': getattr(member, 'name', None),
                    'nickname': getattr(member, 'nick', None)} for member in matches]}

    await search(look_in, list(range(len(queries))))
    misses = [index for index, row in enumerate(resolutions) if row['found_in'] is None]
    unknown = {index for index in misses if resolutions[index]['status'] == 'unknown'}
    if misses:
        await search('discord' if look_in == 'game' else 'game', misses)
        for index in unknown:
            if resolutions[index]['status'] == 'not_found':
                resolutions[index]['status'] = 'unknown'
    links = (await asyncio.to_thread(context.account_links.get_links_for_members, sorted(members))
             if members and context.account_links else {})
    await require_evidence_access(context)
    selected, selected_members, resolved_members = {}, set(), set()
    for resolution in resolutions:
        resolution['look_in'] = look_in
        matches = resolution.setdefault('matching_members', [])
        for member in matches:
            identifier = member['member_id']
            member['accounts'] = [{'player_tag': row['player_tag'],
                                   'player_name': row.get('player_name_last_seen')}
                                  for row in links.get(identifier, [])]
            member['account_links_status'] = ('links_unavailable' if context.account_links is None
                else 'available' if member['accounts'] else 'no_linked_accounts')
        chosen = []
        if resolution['found_in'] is not None:
            if resolution['found_in'] == 'game':
                chosen = [dict(row) for row in resolution['candidates']]
            else:
                chosen = [{**row, 'member_id': member['member_id'],
                           'member_name': member['member_name']}
                          for member in matches for row in member['accounts']]
            identifiers = {row['member_id'] for row in chosen}
            identifiers.update(member['member_id'] for member in matches)
            selected_members.update(identifiers - {None})
            if resolution['status'] != 'resolved':
                for row in chosen:
                    row['candidate_for'] = [resolution['query']]
            if (resolution['status'] == 'resolved' and len(identifiers) == 1
                    and getattr(context.guild, 'chunked', True) is not False):
                identifier = next(iter(identifiers))
                member = context.guild.get_member(identifier) if identifier else None
                if (member is not None and member.id == identifier and identifier not in TEST_ACCOUNT_IDS
                        and not getattr(member, 'bot', False)):
                    resolved_members.add(identifier)
        resolution['selected_accounts'] = chosen
        for row in chosen:
            previous = selected.get(row['player_tag'], {})
            queries = list(dict.fromkeys(previous.get('candidate_for', []) + row.get('candidate_for', [])))
            selected[row['player_tag']] = {**row, **({'candidate_for': queries} if queries else {})}
    return {'accounts': selected, 'member_ids': sorted(selected_members),
            'resolved_member_ids': sorted(resolved_members),
            'name_resolutions': resolutions, 'missing_sources': missing,
            'selection_complete': all(row['found_in'] is not None for row in resolutions)}

__all__ = ["matching_accounts", "normalized_name", "edit_distance", "name_segments",
           "word_prefix", "small_spelling_edit", "resolve_account_names", "resolve_names"]
