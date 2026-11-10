"""Each stored attack in the selected wars: who hit which base, and how it went."""

import asyncio

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import core as read_core
from . import record_wars
from .cache import source_revision
from .wars import selected_wars

# Wars per record read, and the most tags one read filters on.
BATCH = 500
MAX_TAGS = 200
MEANINGS = {
    'map_position': "The attacker's place on that war's map; 1 is the top base.",
    'defender_map_position': "The attacked base's place on the opponent's map; null when the record cannot place it.",
    'war_end_at': 'When the war ended, or ends for a war still under way.'}


async def selected_accounts(context, arguments):
    """The chosen account tags, or None for every account; members are read through their links."""
    if arguments.get('cohort_ref'):
        from .projections import cohort_selection
        selected = await cohort_selection(context, arguments['cohort_ref'], 'account_tags')
        if 'error' in selected:
            return selected
        arguments = {**arguments, **selected}
    if arguments.get('member_ids'):
        if context.account_links is None:
            return {'error': 'Account links are not available.', 'cause': 'missing_setup'}
        links = await asyncio.to_thread(context.account_links.get_links_for_members, arguments['member_ids'])
        tags = {link['player_tag'] for values in links.values() for link in values}
        return {'tags': tags, 'members_without_links': sorted(
            member for member in arguments['member_ids'] if not links.get(member))}
    if arguments.get('account_tags'):
        return {'tags': set(arguments['account_tags']), 'members_without_links': []}
    return {'tags': None, 'members_without_links': []}


def attack_row(row, codes, owners):
    tag = row['attacker_tag']
    return {'war_id': row['war_id'], 'war_type': row['type'], 'war_state': row['state'],
            'war_end_at': record_wars.iso(row['end_at']), 'clan_code': codes.get(row['clan_tag']),
            'opponent_tag': row['opponent_tag'], 'opponent_name': row['opponent_name'],
            'player_tag': tag, 'player_name': row['attacker_name'], 'member_id': owners.get(tag),
            'map_position': row['attacker_map_position'], 'townhall': row['attacker_townhall'],
            'defender_map_position': row['defender_position'], 'defender_townhall': row['defender_townhall'],
            'defender_basis': row['defender_basis'], 'stars': row['stars'], 'destruction': row['destruction'],
            'attack_order': row['attack_order'], 'duration_seconds': row['duration']}


async def war_attacks(context, arguments):
    """The attacks family accounts made in the wars war attendance would select."""
    accounts = await selected_accounts(context, arguments)
    if 'error' in accounts:
        return accounts
    reads = record_wars.war_reads(context)
    if reads is None:
        return {'error': 'Stored war evidence is not available.', 'cause': 'missing_setup'}
    war_type = arguments['war_type']
    source = await selected_wars(context, arguments, war_type, running=True)
    if 'error' in source:
        return source
    tags = accounts['tags']
    wars = {(row['clan_code'], row['war_id']) for row in source['rows']
            if tags is None or row['player_tag'] in tags}
    if tags is None:
        wars.update((war['clan_code'], war['war_id']) for war in source.get('war_aliases', ()))
    codes = {CLANS[code].tag: code for code in CLANS}
    pairs = sorted((CLANS[code].tag, war) for code, war in wars)
    dependencies = getattr(context, 'read_dependencies', None)
    if dependencies is not None:
        # Taken before the read, as the other war reads do.
        revision = (await asyncio.to_thread(source_revision, reads)
                    if getattr(context, 'read_cache', None) is not None else None)
        dependencies.observe(context, reads, revision)
    filtered = tags is not None and len(tags) <= MAX_TAGS
    rows, missing, incomplete, checked = [], [], [], 0
    try:
        now = read_core.now_us()
        for start in range(0, len(pairs), BATCH):
            part = await reads.selected_attacks(pairs[start:start + BATCH], as_of=now,
                                                player_tags=sorted(tags) if filtered else None)
            rows.extend(row for row in part['rows'] if tags is None or row['attacker_tag'] in tags)
            missing.extend(part['wars_not_found'])
            incomplete.extend(part['incomplete_wars'])
            checked += part['wars_checked']
    except record_wars.FAILURES:
        return dict(record_wars.UNAVAILABLE)
    except ValueError as error:
        return {'error': str(error), 'cause': 'invalid_arguments'}
    owners = {}
    attackers = sorted({row['attacker_tag'] for row in rows if row['attacker_tag']})
    if attackers and context.account_links is not None:
        linked = await asyncio.to_thread(context.account_links.get_links_by_tags, attackers)
        owners = {tag: value['discord_user_id'] for tag, value in linked.items() if value}
    rows.sort(key=lambda row: (-row['end_at'], row['clan_tag'], row['war_id'], row['attack_order']))
    result = [attack_row(row, codes, owners) for row in rows]
    attacked = {row['player_tag'] for row in result}
    return {'rows': result, 'war_type': war_type,
            **({'accounts_without_attacks': sorted(tags - attacked)} if tags is not None else {}),
            **({'members_without_links': accounts['members_without_links']}
               if accounts['members_without_links'] else {}),
            'coverage': {'wars_checked': checked,
                         'wars_not_found': [{'war_id': war['war_id'], 'clan_code': codes.get(war['clan_tag'])}
                                            for war in missing],
                         'incomplete_wars': [{'war_id': war['war_id'], 'clan_code': codes.get(war['clan_tag']),
                                              'causes': war['causes']} for war in incomplete],
                         'wars_without_rosters': source.get('wars_without_rosters', []),
                         'as_of': record_wars.iso(now),
                         'basis': 'Stored attacks by family accounts in the selected wars; '
                                  'an incomplete war may be missing attacks.'},
            'ownership_basis': 'current bot links; historical ownership unknown',
            'ordering': 'war end descending, then clan, war and attack order',
            'meanings': MEANINGS}
