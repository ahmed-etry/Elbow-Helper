"""Complete member/account joins, clan rosters and distinct Discord cohorts."""
import asyncio
import json
from collections import Counter
from datetime import datetime
from dataclasses import replace
import time

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from elbow_helper.configuration.roles import MEMBER_ROLE_ID, TRIAL_ROLE_ID
from elbow_helper.domain.player_tags import normalize_player_tag
from .core import family_tool, IDS, TAGS, DATE
from .war_reads import CLANS_INPUT
from .projections import ACCOUNT_FILTERS, COHORT_REF, PERFORMANCE, account_matches, cohort_selection, performance_projection, unknown_account_filter, unknown_account_data
from ..access import require_evidence_access
from ..datasets.clash import read_clash
from ..research.members import _complete_members, _joined_at
from ..engine.capability_contract import CapabilityContract
from ..engine.budgets import TOOL_TIMEOUT_SECONDS
from .member_context import owner_context, account_owner_profiles, configured_member_roles, PROFILE_ROLE_GROUPS
from ..read_cache import stored_read
from .. import record_identity

CLAN_ROLES = {'leadership': 'leadership_role_id', 'member': 'member_role_id',
              'war': 'war_role_id', 'cwl': 'cwl_role_id', 'cwl_helper': 'cwl_helper_role_id'}
DEFAULT_ROSTER_BASIS = 'community'
ROSTER_BASIS = {'type': 'string', 'enum': ['community', 'current_in_game', 'seasonal_cwl'],
    'description': 'current_in_game: accounts in the clan in game right now; community: home assignments by '
                   'Discord role, including assigned accounts temporarily elsewhere; seasonal_cwl: a season\'s CWL lineup.'}
EXCLUDE_LEADERSHIP = {'type': 'boolean', 'description':
    'For clan rosters, exclude leader/co-leader accounts and all selected accounts of their linked owners, '
    'plus active/sleeping Discord leadership owners. Unknown owners remain explicitly unverified.'}


def entry_settings(data, status):
    settings = {field: data.get(field) if status == 'ok' else None for field in
                ('type', 'requiredTownhallLevel', 'requiredTrophies', 'requiredBuilderBaseTrophies')}
    meaning = {
        'open': ('direct_join', 'Players meeting the live requirements can join directly.'),
        'inviteOnly': ('request_approval', 'Players meeting the live requirements can send join requests; clan leadership approves them.'),
        'closed': ('invitation_required', 'Join requests are blocked; an invitation from clan leadership is required.'),
    }.get(settings['type'])
    return {**settings, 'join_method': meaning[0] if meaning else None,
            'type_meaning': meaning[1] if meaning else None}


async def profiles(context, tags):
    result = {}
    for start in range(0, len(tags), 200):
        response = await read_clash(context, {'kind': 'player', 'tags': tags[start:start+200],
            'fields': ['tag', 'name', 'townHallLevel', 'role', 'leagueTier', 'league', 'clan', 'trophies', 'donations', 'warStars'],
            'limit': 50})
        if 'error' in response:
            result.update({tag: {'tag': tag, 'status': response.get('cause', 'unavailable')}
                           for tag in tags[start:start+200]})
        else:
            result.update({row['tag']: row for row in response['items']})
    return result


def member_fields(member, identifier, *, include_roles=False, selected_role_ids=()):
    held = {role.id for role in member.roles} if member else set()
    role_names = {role.id: role.name for role in member.roles} if member else {}
    bindings = [{'clan_code': code, 'name': CLANS[code].name, 'tag': CLANS[code].tag,
              'role_id': CLANS[code].member_role_id,
              'role_name': role_names[CLANS[code].member_role_id]}
             for code in CLAN_ORDER if CLANS[code].member_role_id in held]
    homes = [clan for clan in bindings if not CLANS[clan['clan_code']].is_utility]
    participation = [clan for clan in bindings if CLANS[clan['clan_code']].is_utility]
    return {**owner_context(member, identifier),
            'nickname': getattr(member, 'nick', None) if member else None,
            'joined_at': _joined_at(member) if member else None,
            'family_membership_roles': {
                'member': {'role_id': MEMBER_ROLE_ID, 'held': MEMBER_ROLE_ID in held if member else None},
                'trial': {'role_id': TRIAL_ROLE_ID, 'held': TRIAL_ROLE_ID in held if member else None}},
            'home_clans': homes,
            'participation_clans': participation,
            'home_clan_status': ('not_in_guild' if member is None else 'multiple' if len(homes) > 1
                                 else 'resolved' if homes else 'no_home_role'),
            'home_clan_basis': 'configured non-utility Discord clan member roles, assigned at acceptance and '
                'changed with promotion or demotion within the family; home roles define where accounts '
                'are supposed to be; a member with accounts in different home clans holds multiple home '
                'roles; roles never follow in-game moves or become stale because of them; an account '
                'outside its home clan has left or moved; utility roles are extra participation; '
                'not historical acceptance, current account location or identification of a main account',
            **({'roles': [role for role in configured_member_roles(member, requested_role_ids=selected_role_ids)
                          if include_roles or role['role_id'] in selected_role_ids]}
               if include_roles or selected_role_ids else {})}


async def member_accounts(context, arguments):
    roster_clans, roster_names = {}, {}
    name_selection = {}
    name_identifiers = []
    resolved_name_identifiers = []
    selected_accounts = {}
    if arguments.get('after') or arguments.get('before'):
        from .war_reads import period
        try:
            period({**arguments, 'before': arguments.get('before')
                    or context.source_message.created_at.isoformat()})
        except ValueError as error:
            return {'error': str(error), 'cause': 'invalid_arguments'}
    name_selector = 'account_names' if arguments.get('account_names') else 'name' if arguments.get('name') else None
    if name_selector:
        conflicts = [key for key in ('member_ids', 'account_tags', 'name', 'cohort_ref')
                     if key != name_selector and arguments.get(key)]
        if conflicts:
            return {'error': name_selector + ' cannot be combined with ' + ', '.join(conflicts) + '. '
                    'Use name for one lookup or account_names for several with look_in. '
                    'Use separate plan steps for other selectors.',
                    'cause': 'invalid_arguments', 'conflicting_fields': [name_selector, *conflicts]}
        from .account_names import resolve_names
        name_selection = await resolve_names(context, arguments.get('account_names') or [arguments['name']],
                                             arguments.get('look_in'))
        if 'error' in name_selection:
            return name_selection
        selected_accounts = name_selection.pop('accounts')
        roster_names = {tag: row['player_name'] for tag, row in selected_accounts.items()}
        name_identifiers = name_selection.pop('member_ids')
        resolved_name_identifiers = name_selection.pop('resolved_member_ids')
        if not roster_names and not name_identifiers:
            return {'rows': [], 'members': [], 'status': 'needs_input',
                    'resolved_member_ids': [], **name_selection}
        arguments = {key: value for key, value in arguments.items() if key not in ('name', 'account_names')}
        arguments['account_tags'] = sorted(roster_names)
    if arguments.get('cohort_ref'):
        selection = await cohort_selection(context,arguments['cohort_ref'],'member_ids')
        if 'error' in selection:
            return selection
        report = context.state.reports[arguments['cohort_ref']]
        if report.family == 'read_clan_rosters' and arguments.get('include_roles'):
            for row in json.loads(report.payload)['rows']:
                if row.get('player_tag') and row.get('clan_code') in CLANS:
                    roster_clans.setdefault(row['player_tag'], set()).add(row['clan_code'])
                    roster_names[row['player_tag']] = row.get('player_name')
            await _complete_members(context.guild)
        arguments = {**arguments,**selection}
    selectors = [key for key in ('member_ids', 'account_tags', 'name') if arguments.get(key)]
    if not name_selection and len(selectors) != 1:
        return {'error': 'Which members, accounts or exact name do you want?',
                'cause': 'invalid_arguments',
                **({'invalid_fields': selectors} if selectors else
                   {'missing_fields': ['member_ids', 'account_tags', 'name', 'account_names', 'cohort_ref']})}
    identifiers = list(arguments.get('member_ids', []))
    tags = sorted({tag for value in arguments.get('account_tags', [])
                   if (tag := normalize_player_tag(value)) is not None})
    if arguments.get('account_tags') and len(tags) != len(set(arguments['account_tags'])):
        return {'error': 'Choose valid, distinct account tags.', 'cause': 'invalid_arguments'}
    links = {}
    if context.account_links:
        if identifiers:
            links = await asyncio.to_thread(context.account_links.get_links_for_members, identifiers)
            tags = sorted({row['player_tag'] for rows in links.values() for row in rows})
        else:
            reverse = await asyncio.to_thread(context.account_links.get_links_by_tags, tags)
            for tag, row in reverse.items():
                if row:
                    links.setdefault(row['discord_user_id'], []).append(row)
            identifiers = sorted(links)
    identifiers = sorted(set(identifiers) | set(name_identifiers))
    current = await profiles(context, tags)
    histories = await record_identity.account_histories(context, tags) if arguments.get('include_history') else {}
    owners = {row['player_tag']: identifier for identifier, rows in links.items() for row in rows}
    owners.update({tag: row['member_id'] for tag, row in selected_accounts.items()
                   if row.get('member_id') is not None})
    identifiers = sorted(set(identifiers) | set(owners.values()))
    members = await account_members(context, identifiers,
        links=links if arguments.get('member_ids') else None, include_roles=arguments.get('include_roles', False))
    members_by_id = {row['member_id']: row for row in members}
    for resolution in name_selection.get('name_resolutions', []):
        if resolution['status'] == 'resolved':
            continue
        for candidate in resolution.get('matching_members', []) + resolution.get('candidates', []):
            profile = members_by_id.get(candidate.get('member_id'))
            if profile is not None and resolution['query'] not in profile.setdefault('candidate_for', []):
                profile['candidate_for'].append(resolution['query'])
                context.state.candidate_member_ids.add(profile['member_id'])
    result = []
    for tag in tags:
        item = current.get(tag, {'status': 'unavailable'})
        data = item.get('data', {})
        identifier = owners.get(tag)
        member = context.guild.get_member(identifier) if identifier else None
        checks = [{'clan_code': code, 'role_id': CLANS[code].member_role_id,
                   'has_role': (CLANS[code].member_role_id in {role.id for role in member.roles}
                                if member and CLANS[code].member_role_id is not None else None)}
                  for code in sorted(roster_clans.get(tag, ()))]
        if checks and identifier in members_by_id:
            profile = members_by_id[identifier]
            profile['home_role_checks'] = list({check['clan_code']: check for check in
                profile.get('home_role_checks', []) + checks}.values())
        result.append({'member_id': identifier, 'member_name': member.display_name if member else None,
            'player_tag': tag, 'player_name': data.get('name') or roster_names.get(tag),
            'townHallLevel': data.get('townHallLevel'),
            'role': data.get('role'), 'ranked_league': data.get('leagueTier'), 'legacy_league': data.get('league'),
            'clan': data.get('clan'), 'trophies': data.get('trophies'),
            **({'clan_status': data['clan_status']} if 'clan_status' in data else {}),
            'donations': data.get('donations'),
            'war_stars': data.get('warStars'), 'observed_at': item.get('observed_at'),
            'profile_status': item['status'],
            **({'candidate_for': selected_accounts[tag]['candidate_for']}
               if selected_accounts.get(tag, {}).get('candidate_for') else {})})
        if tag in histories:
            result[-1]['record_history'] = histories[tag]
    for identifier in identifiers:
        member = context.guild.get_member(identifier)
        if not links.get(identifier):
            result.append({'member_id': identifier, 'member_name': member.display_name if member else None,
                           'player_tag': None,
                           'profile_status': 'unlinked' if context.account_links else 'links_unavailable',
                           **({'candidate_for': members_by_id[identifier]['candidate_for']}
                              if members_by_id[identifier].get('candidate_for') else {})})
    await require_evidence_access(context)
    matched = [row for row in result if account_matches(row,arguments)]
    history_reads = {}
    if arguments.get('after'):
        from .account_activity import account_activity
        history_reads = await account_activity(context, matched, arguments)
    from .domain_facts import clan_profile
    current_clans = {row.get('clan', {}).get('tag') for row in matched if row.get('clan')}
    await require_evidence_access(context)
    for identifier in resolved_name_identifiers if name_selection else identifiers:
        # Following candidate IDs into another read must not promote them to
        # unique action subjects. A subsequent unique name lookup can resolve one.
        if not name_selection and identifier in context.state.candidate_member_ids:
            continue
        member = context.guild.get_member(identifier)
        if member:
            context.state.resolved_members[identifier] = member.display_name
    return {'rows': sorted(matched, key=lambda row: (row['member_id'] is None,
            row['member_id'] or 0, row.get('player_tag') or '')),
            'members': members,
            **({'resolved_member_ids': resolved_name_identifiers} if name_selection else {}),
            **({'configured_role_groups': list(PROFILE_ROLE_GROUPS)} if arguments.get('include_roles') else {}),
            'status': 'needs_input' if name_selection and not name_selection['selection_complete'] else 'resolved',
            'member_count': len(identifiers), 'account_count':None if any(row.get('profile_status')=='links_unavailable' for row in result) else sum(bool(row.get('player_tag')) for row in matched),
            'unknown_profile_count':sum(unknown_account_data(row,arguments) for row in result),
            'unknown_accounts':[row for row in result if unknown_account_data(row,arguments)],
            'count_complete':not any(unknown_account_data(row,arguments) for row in result),
            'clan_facts': [clan_profile(code) for code, clan in CLANS.items() if clan.tag in current_clans],
            **({'home_role_comparisons': [{key: row[key] for key in (
                    'member_id', 'member_name', 'player_tag', 'player_name')} |
                    {'clan_codes': sorted(roster_clans.get(row.get('player_tag'), ()))} for row in matched],
                'role_comparison_complete': len(matched) == len(roster_clans)} if roster_clans else {}),
            **{key: value for key, value in name_selection.items() if key != 'accounts'},
            **({'history_reads': history_reads} if arguments.get('after') else {}),
            'coverage': ('Names search look_in first, falling back only on a miss; found_in identifies '
                         'the matching side; members holds Discord profiles and all linked account tags'
                         if name_selection else
                         'selected game accounts only; owners do not expand the account selection'
                         if arguments.get('account_tags') else
                         'all bot-linked accounts for selected members; missing profiles remain unknown')}


async def account_members(context, identifiers, *, links=None, include_roles=False):
    """Keep each linked owner's Discord facts once, separate from game rows."""
    identifiers = sorted(set(identifiers) - {None})
    if links is None:
        links = (await asyncio.to_thread(context.account_links.get_links_for_members, identifiers)
                 if identifiers and context.account_links else {})
    await require_evidence_access(context)
    result = []
    for identifier in identifiers:
        member = context.guild.get_member(identifier)
        result.append({**member_fields(member, identifier, include_roles=include_roles),
            'membership_status': 'present' if member else 'not_in_guild',
            'has_elbow_helper_links': bool(links.get(identifier)) if context.account_links else None,
            'account_tags': sorted({row['player_tag'] for row in links.get(identifier, [])}),
            'account_links_status': 'available' if context.account_links else 'links_unavailable'})
    return result


async def seasonal_tags(context, code, season):
    source = await read_clash(context, {'kind': 'cwl_group', 'tags': [CLANS[code].tag], 'detail': 'full'})
    if 'error' not in source:
        data = source['items'][0].get('data', {})
        if data.get('season') and (season is None or season == data['season']):
            clan = next((row for row in data.get('clans', []) if row['tag'] == CLANS[code].tag), None)
            if clan:
                return (sorted({row['tag'] for row in clan.get('members', [])}), data['season'],
                        'league_group_cohort', source.get('observed_at'))
    if context.cwl_queries:
        snapshot = await stored_read(context, context.cwl_queries, 'performance', history_limit=1,
                                            season=season, clan_code=code)
        if not snapshot.rows and not snapshot.seasons:
            return [], season, 'season_cohort_unavailable'
        return sorted({row.player_tag for row in snapshot.rows}), \
               season or (snapshot.seasons[0] if snapshot.seasons else None), 'stored_completed_cwl_participants'
    return [], season, 'season_cohort_unavailable'


async def clan_rosters(context, arguments):
    from .domain_facts import clan_profile
    deadline = time.monotonic() + TOOL_TIMEOUT_SECONDS - 3
    context = replace(context, deadline_monotonic=min(deadline, context.deadline_monotonic)
                      if context.deadline_monotonic is not None else deadline)
    codes = arguments.get('clan_codes', list(CLANS))
    basis = arguments.get('basis', DEFAULT_ROSTER_BASIS)
    result, summaries = [], []
    current = await read_clash(context, {'kind': 'clan', 'tags': [CLANS[code].tag for code in codes],
                   'member_scope': 'current', 'detail': 'full', 'limit': 50})
    by_tag = {item['tag']: item for item in current.get('items', [])}
    leadership_tags = {row['tag'] for item in by_tag.values() if item.get('status') == 'ok'
                       for row in item.get('data', {}).get('memberList', [])
                       if row.get('role') in ('leader', 'coLeader')}
    for code in codes:
        clan = CLANS[code]
        item = by_tag.get(clan.tag, {'status': 'unavailable'})
        data = item.get('data', {})
        selected_season, source_basis = arguments.get('season'), 'current_in_game'
        cohort_observed_at = item.get('observed_at')
        roster = [dict(row, observed_at=item.get('observed_at')) for row in data.get('memberList', [])]
        if basis == 'seasonal_cwl' or clan.cwl_only and basis == 'community':
            seasonal = await seasonal_tags(context, code, selected_season)
            tags, selected_season, source_basis = seasonal[:3]
            cohort_observed_at = seasonal[3] if len(seasonal) > 3 else None
            found = await profiles(context, tags)
            roster = [dict(found[tag].get('data', {}), tag=tag, profile_status=found[tag]['status'],
                           observed_at=min((value for value in (found[tag].get('observed_at'),
                               cohort_observed_at) if value), default=None)) for tag in tags]
        original_count = len(roster)
        if (arguments.get('include_war_stars') or arguments.get('ranked_tier_ids') or arguments.get('ranked_tier_prefix')) and source_basis=='current_in_game':
            found = await profiles(context,[row['tag'] for row in roster])
            roster = [dict(row,warStars=found.get(row['tag'],{}).get('data',{}).get('warStars'),
                ranked_league=found.get(row['tag'],{}).get('data',{}).get('leagueTier'),
                observed_at=min((value for value in (row.get('observed_at'),
                    found.get(row['tag'], {}).get('observed_at')) if value), default=None)) for row in roster]
        roster = [dict(row,ranked_league=row.get('ranked_league',row.get('leagueTier'))) for row in roster]
        leadership_tags.update(row['tag'] for row in roster
                               if row.get('role') in ('leader', 'coLeader'))
        unknown_filters = sum(unknown_account_filter(row,arguments) for row in roster)
        roster = [row for row in roster if account_matches(row,arguments)
                  and row.get('role') not in arguments.get('exclude_in_game_roles',[])]
        distribution = Counter(row.get('townHallLevel') for row in roster)
        # Clash clan profiles omit capacity; the game's clan limit is 50.
        capacity = data.get('capacity', 50) if item['status'] == 'ok' else None
        summaries.append({'clan_code': code, 'name': data.get('name'), 'tag': clan.tag,
            'observed_at': min((value for value in (item.get('observed_at'), cohort_observed_at)
                                if value), default=None),
            'entry_settings': entry_settings(data, item['status']),
            'profile':clan_profile(code), 'basis': source_basis, 'season': selected_season,
            'status': item['status'] if source_basis == 'current_in_game' else source_basis,
            'cohort_accounts':len(roster) if (source_basis=='current_in_game' and item['status']=='ok'
                or source_basis in ('league_group_cohort','stored_completed_cwl_participants')) else None,
            'included_accounts':len(roster),'excluded_accounts':original_count-len(roster),
            'unknown_filter_accounts':unknown_filters,
            'in_game_members': data.get('members'), 'in_game_capacity':capacity,
            'capacity_basis': 'clan_member_limit' if 'capacity' not in data and capacity is not None else 'profile',
            'open_in_game_slots': max(0,capacity-data['members'])
             if type(data.get('members')) is int and type(capacity) is int else None,
            'town_halls': {str(key): value for key, value in sorted(distribution.items(), key=lambda pair: (pair[0] is None, pair[0] or 0))}})
        if not roster and (item['status'] != 'ok' or source_basis=='season_cohort_unavailable'):
            result.append({'clan_code': code, 'player_tag': None, 'profile_status': item['status'], 'basis': source_basis})
        for row in roster:
            result.append({'clan_code': code, 'basis': source_basis, 'season': selected_season,
                'player_tag': row['tag'], 'player_name': row.get('name'), 'townHallLevel': row.get('townHallLevel'),
                'role': row.get('role'), 'donations': row.get('donations'),
                'donations_received':row.get('donationsReceived'),'war_stars':row.get('warStars'),
                'observed_at': row.get('observed_at'),
                'profile_status': row.get('profile_status', 'ok')})
    sort = arguments.get('sort','town_hall')
    tags = sorted({row['player_tag'] for row in result if row.get('player_tag')})
    if arguments.get('exclude_leadership'):
        tags = sorted(set(tags) | leadership_tags)
    links = (await asyncio.to_thread(context.account_links.get_links_by_tags, tags)
             if context.account_links and tags else {})
    for row in result:
        identifier = (links.get(row.get('player_tag')) or {}).get('discord_user_id')
        member = context.guild.get_member(identifier) if identifier else None
        row.update(owner_context(member, identifier), account_purpose=None)
    leadership_filter = None
    if arguments.get('exclude_leadership'):
        leadership_owners = {(links.get(tag) or {}).get('discord_user_id') for tag in leadership_tags}
        leadership_owners.discard(None)
        leadership_owners.update(row['member_id'] for row in result
            if row.get('leadership', {}).get('status') in ('active', 'sleeping') and row['member_id'] is not None)
        retained = [row for row in result if row.get('role') not in ('leader', 'coLeader')
                    and row.get('member_id') not in leadership_owners]
        leadership_filter = {'excluded_accounts': len(result) - len(retained),
            'unverified_accounts': sum(row.get('player_tag') is not None
                and row.get('membership_status') != 'present' for row in retained),
            'basis': 'Current Discord leadership and observed leader/co-leader ranks across the selected '
                'rosters; linked-owner exclusions apply across accounts. Unresolved owners remain unverified.'}
        for summary in summaries:
            accounts = [row for row in retained if row['clan_code'] == summary['clan_code']
                        and row.get('player_tag')]
            removed = summary['included_accounts'] - len(accounts)
            summary['included_accounts'] = len(accounts)
            summary['excluded_accounts'] += removed
            if summary['cohort_accounts'] is not None:
                summary['cohort_accounts'] = len(accounts)
            summary['unknown_filter_accounts'] += sum(row.get('membership_status') != 'present'
                                                      for row in accounts)
            distribution = Counter(row.get('townHallLevel') for row in accounts)
            summary['town_halls'] = {str(key): value for key, value in sorted(distribution.items(),
                key=lambda pair: (pair[0] is None, pair[0] or 0))}
        result = retained
    for summary in summaries:
        accounts = [row for row in result if row['clan_code'] == summary['clan_code'] and row.get('player_tag')]
        owners = {}
        for row in accounts:
            if row['member_id'] is not None:
                group = owners.setdefault(row['member_id'], {'member_id': row['member_id'],
                    'member_name': row['member_name'], 'account_count': 0})
                group['account_count'] += 1
        summary['ownership'] = {
            'linked_accounts': sum(row['member_id'] is not None for row in accounts),
            'unknown_owner_accounts': sum(row['member_id'] is None for row in accounts),
            'distinct_linked_owners': len(owners), 'owners': list(owners.values()),
            'basis': 'accounts in this roster only; ownership does not identify mains or alts',
        }
    def order(row):
        field = {'town_hall':'townHallLevel','donations':'donations','war_stars':'war_stars'}.get(sort)
        value = row.get(field) if field else 0
        return (value is None,-value if value is not None else 0,CLAN_ORDER.index(row['clan_code']),row.get('player_tag') or '')
    members = account_owner_profiles(result)
    return {'rows': sorted(result, key=order), 'members': members,
            'count':sum(bool(row.get('player_tag')) for row in result),
            'count_complete':all(not row['unknown_filter_accounts'] and row['cohort_accounts'] is not None for row in summaries),
            **({'leadership_filter': leadership_filter} if leadership_filter is not None else {}),
            'clans': summaries, 'coverage': 'seasonal cohorts and current in-game rosters have distinct bases'}


async def member_cohort(context, arguments):
    basis = arguments['basis']
    if arguments.get('role_name') and basis != 'role_holders':
        return {'error': 'Use role_holders to select a named role.', 'cause': 'invalid_arguments'}
    if basis == 'clan_rosters':
        from .cohort_activity import roster_cohort
        return await roster_cohort(context, arguments)
    if arguments.get('exclude_leadership'):
        return {'error': 'exclude_leadership requires basis=clan_rosters.', 'cause': 'invalid_arguments'}
    if basis != 'selected' and any(key in arguments for key in ('member_ids', 'cohort_ref')):
        return {'error': 'Use basis=selected for a chosen set.', 'cause': 'invalid_arguments'}
    accepted, selected_accounts = {}, None
    if basis == 'selected':
        if bool(arguments.get('member_ids')) == bool(arguments.get('cohort_ref')):
            return {'error': 'Choose member_ids or a read_member_accounts cohort_ref.', 'cause': 'invalid_arguments'}
        if any(key in arguments for key in ('joined_after', 'joined_before', 'accepted_after', 'accepted_before',
                                           'role_ids', 'role_name', 'clan_role')):
            return {'error': 'Choose the member or account set without membership filters.', 'cause': 'invalid_arguments'}
        if arguments.get('cohort_ref'):
            selection = await cohort_selection(context, arguments['cohort_ref'], 'member_ids')
            if 'error' in selection:
                return selection
            report = context.state.reports[arguments['cohort_ref']]
            if report.guild_id != context.guild.id or report.family != 'read_member_accounts':
                return {'error': 'Choose a read_member_accounts result.', 'cause': 'invalid_arguments'}
            # Use only the selected rows, never the owner's complete account_tags.
            selected_accounts = json.loads(report.payload)['rows']
            ids = sorted({row['member_id'] for row in selected_accounts if row.get('member_id')})
        else:
            ids = sorted(set(arguments['member_ids']))
        coverage = 'Explicitly selected members or selected account rows and their owners; owners do not expand the account selection.'
    elif basis == 'accepted_recruitment':
        from .workflow_reads import active_trial_rows
        result = await active_trial_rows(context)
        if 'error' in result:
            return result
        trials = result['rows']
        accepted = {row['applicant_member_id']: row['start_at'] for row in trials}
        ids = sorted({row['applicant_member_id'] for row in trials})
        coverage = 'active accepted trials only; historical acceptance needs ticket evidence'
    else:
        ids, coverage = None, 'complete current Discord membership snapshot; a join is not recruitment acceptance'
    members = await _complete_members(context.guild)
    if basis == 'accepted_recruitment' and any(key in arguments for key in ('joined_after', 'joined_before')):
        return {'error': 'Choose acceptance dates separately from Discord join dates.', 'cause': 'invalid_arguments'}
    date_fields = ('accepted_after', 'accepted_before') if basis == 'accepted_recruitment' else ('joined_after', 'joined_before')
    try:
        after, before = (datetime.fromisoformat(arguments[key]) if arguments.get(key) else None for key in date_fields)
    except ValueError:
        return {'error': 'Choose valid timezone-aware dates.', 'cause': 'invalid_arguments'}
    if any(value is not None and value.utcoffset() is None for value in (after, before)):
        return {'error': 'Choose timezone-aware join dates.', 'cause': 'invalid_arguments'}
    if after and before and after >= before:
        return {'error': 'The join window must end after it starts.', 'cause': 'invalid_arguments'}
    roles = set(arguments.get('role_ids', []))
    home_roles = set()
    if basis == 'role_holders' and arguments.get('role_name'):
        if roles or 'clan_role' in arguments:
            return {'error': 'Choose a role name or role IDs or a configured clan role.', 'cause': 'invalid_arguments'}
        name = arguments['role_name'].strip().casefold()
        matches = [role for role in context.guild.roles if role.name.casefold() == name]
        if len(matches) != 1:
            return {'rows': [], 'status': 'needs_input', 'missing_fields': ['role_id'],
                    'roles': [{'role_id': role.id, 'name': role.name} for role in matches]}
        roles = {matches[0].id}
        home_roles = {CLANS[code].member_role_id for code in arguments.get('clan_codes', [])
                      if CLANS[code].member_role_id is not None}
    elif basis == 'role_holders' and roles and arguments.get('clan_codes'):
        if 'clan_role' in arguments:
            return {'error': 'Choose role IDs or a configured clan role.', 'cause': 'invalid_arguments'}
        home_roles = {CLANS[code].member_role_id for code in arguments['clan_codes']
                      if CLANS[code].member_role_id is not None}
    elif basis == 'role_holders' and arguments.get('clan_codes'):
        field = CLAN_ROLES[arguments.get('clan_role', 'leadership')]
        roles.update(identifier for code in arguments['clan_codes']
                     if (identifier := getattr(CLANS[code], field)) is not None)
    if basis == 'role_holders' and not roles:
        return {'error': 'Which Discord role holders do you want?', 'cause': 'invalid_arguments',
                'missing_fields': ['role_ids', 'role_name', 'clan_codes']}
    rows = []
    selected_members = [(identifier, context.guild.get_member(identifier)) for identifier in ids] if ids is not None else [(member.id, member) for member in members]
    for identifier, member in selected_members:
        joined = (datetime.fromisoformat(accepted[identifier]) if identifier in accepted else None) if basis == 'accepted_recruitment' else getattr(member, 'joined_at', None)
        held = {role.id for role in member.roles} if member else set()
        if (ids is not None and identifier not in ids
                or basis == 'role_holders' and not (roles <= held if arguments.get('role_match') == 'all' else roles & held)
                or home_roles and not home_roles & held
                or after and joined is not None and joined < after
                or before and joined is not None and joined >= before):
            continue
        rows.append({**member_fields(member, identifier, include_roles=arguments.get('include_roles', False),
                                    selected_role_ids=roles if basis == 'role_holders' else ()),
                     'membership_status': 'present' if member else 'not_in_guild', 'selection_status':
                     'unknown_date' if (after or before) and joined is None else 'selected',
                     **({'accepted_at': accepted.get(identifier)}
                     if basis == 'accepted_recruitment' else {})})
    reverse = arguments.get('sort') == 'newest_first'
    sort_field = 'accepted_at' if basis == 'accepted_recruitment' else 'joined_at'
    rows.sort(key=lambda row: (row.get(sort_field) is None,
        (-1 if reverse else 1) * datetime.fromisoformat(row[sort_field]).timestamp() if row.get(sort_field) else 0,
        row['member_id']))
    account_count, unknown_accounts_total = None, 0
    if basis in ('joined_discord', 'selected') or arguments.get('include_accounts') or arguments.get('include_activity') or any(key in arguments for key in ACCOUNT_FILTERS):
        if selected_accounts is not None:
            joined_accounts = {'rows': selected_accounts}
        elif rows:
            joined_accounts = await member_accounts(context, {'member_ids': [row['member_id'] for row in rows],
                                                              'include_history': arguments.get('include_history', False)})
        else:
            joined_accounts = {'rows': []}
        if 'error' in joined_accounts:
            return joined_accounts
        by_member = {}
        for account in joined_accounts['rows']:
            by_member.setdefault(account['member_id'],[]).append(account)
        for row in rows:
            # Owner identity and Discord roles are retained once on the member,
            # not repeated on every alt in this complete cohort snapshot.
            owner_fields = {'leadership', 'member_id', 'member_name', 'nickname', 'joined_at', 'roles',
                            'home_clans', 'participation_clans', 'home_clan_status', 'home_clan_basis',
                            'family_membership_roles', 'has_elbow_helper_links'}
            row['has_elbow_helper_links'] = (any(a.get('player_tag') for a in by_member.get(row['member_id'], []))
                                            if context.account_links is not None else None)
            row['accounts'] = [{key: value for key, value in account.items() if key not in owner_fields}
                               for account in by_member.get(row['member_id'],[]) if account_matches(account,arguments)]
            row['unknown_account_profiles'] = sum(unknown_account_data(account,arguments) for account in by_member.get(row['member_id'],[]))
        if selected_accounts is not None:
            for account in selected_accounts:
                if account.get('member_id') is None and account_matches(account, arguments):
                    rows.append({**member_fields(None, None), 'membership_status': 'unlinked',
                        'selection_status': 'selected', 'accounts': [dict(account)],
                        'unknown_account_profiles': int(unknown_account_data(account, arguments))})
        unknown_accounts_total = sum(row['unknown_account_profiles'] for row in rows)
        if any(key in arguments for key in ACCOUNT_FILTERS):
            rows = [row for row in rows if any(account.get('player_tag') for account in row['accounts'])]
        account_count = sum(bool(account.get('player_tag')) for row in rows for account in row['accounts'])
    performance = None
    if arguments.get('performance'):
        scope = ({'account_tags': {account['player_tag'] for row in rows
                  for account in row.get('accounts', []) if account.get('player_tag')}}
                 if selected_accounts is not None else {})
        performance = await performance_projection(context, rows, arguments['performance'], **scope)
    activity = None
    if arguments.get('include_activity'):
        from .cohort_activity import activity_projection, activity_fields
        activity = await activity_projection(context, rows, arguments)
        if 'error' in activity:
            return activity
        activity_fields(rows)
    return {'rows': rows, 'basis': basis,
            **({'configured_role_groups': list(PROFILE_ROLE_GROUPS)} if arguments.get('include_roles') else {}),
            'coverage': coverage, 'member_count': sum(row['member_id'] is not None for row in rows),
            'account_count': account_count,
            'selection_window': {key: arguments.get(key) for key in date_fields},
            'activity_coverage': activity,
            'selected_role_ids': sorted(roles),
            'clan_member_role_ids': sorted(home_roles),
            'count_complete':not unknown_accounts_total and basis != 'accepted_recruitment'
                             and not any(row['selection_status'] == 'unknown_date' for row in rows),
            'historical_acceptance_complete':False if basis == 'accepted_recruitment' else None,
            'unknown_account_profiles':unknown_accounts_total,
            'performance_coverage':performance,
            'unknown_join_dates': sum(row['joined_at'] is None for row in rows)}


TOOLS = (
 family_tool('read_member_accounts', 'Look up names on a chosen side, then join linked accounts and owners.',
     {'member_ids': IDS, 'account_tags': TAGS, 'cohort_ref':COHORT_REF, **ACCOUNT_FILTERS,
      'look_in': {'type': 'string', 'enum': ['game', 'discord'],
          'description': 'Required with name/account_names: game for in-game names and game data; discord for the person in the server (messages, activity, roles, Discord ID); if nothing matches there, the other side is searched.'},
      'account_names': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'uniqueItems': True,
          'items': {'type': 'string', 'minLength': 1, 'maxLength': 100},
          'description': 'Several names using the same lookup as name; requires look_in. Game matches retain spelling tolerance and owners; Discord matches list linked account names and tags. Alternative to name/member_ids/account_tags/cohort_ref; use one selector.'},
      'after': DATE, 'before': DATE,
      'include_roles': {'type': 'boolean', 'default': False,
          'description': "For questions about this member's roles; lists configured clan, leadership, family and war/support roles only."},
      'include_history': {'type': 'boolean', 'default': False,
          'description': "For questions about an account's past owners, clan moves or old names."},
      'name': {'type': 'string', 'minLength': 1, 'maxLength': 100,
               'description': 'One name; requires look_in. Returns matching_members with linked account names and tags or game account candidates with owners. Ambiguity authorizes no actions or pings. Alternative to other selectors.'}}, member_accounts,
     usage_note='look_in: game for in-game names/data; discord for server person (messages, activity, roles, Discord ID); on no match, other side is searched.'),
 family_tool('read_clan_rosters', 'Read every selected clan roster, TH distribution, capacity and donations.',
     {'clan_codes': CLANS_INPUT, 'basis': ROSTER_BASIS,
      'season': {'type': 'string', 'maxLength': 20}, **ACCOUNT_FILTERS,
      'exclude_leadership': EXCLUDE_LEADERSHIP,
      'exclude_in_game_roles':{'type':'array','maxItems':4,'items':{'type':'string','enum':['leader','coLeader','admin','member']}},
      'include_war_stars':{'type':'boolean', 'description':
          'Optional lifetime war stars; requires a player-profile read per account. Use war participation for recent activity.'},
      'sort':{'type':'string','enum':['town_hall','clan','donations','war_stars']}}, clan_rosters,
     usage_note='Default basis is home assignment by Discord role; current_in_game lists who is in game now. exclude_leadership drops leadership owners and alts.'),
 family_tool('read_member_cohort', 'Read named Discord role holders, clan assignments, join dates, trials or roster activity.',
     {'basis': {'type': 'string', 'enum': ['joined_discord', 'role_holders', 'accepted_recruitment', 'clan_rosters', 'selected']},
      'roster_basis': ROSTER_BASIS,
      'exclude_leadership': EXCLUDE_LEADERSHIP,
      'include_activity': {'type': 'boolean', 'description':
          'Selected-account season donations and war activity within after/before; Discord last_active_day (UTC), '
          'active_streak_days and lifetime_messages from all-channel achievements records, independent of that period. '
          'Without after, returns stored Discord activity and marks game war activity not checked.'}, 'after': DATE, 'before': DATE,
      'member_ids': IDS, 'cohort_ref': {**COHORT_REF, 'description':
          'With basis=selected, a read_member_accounts report: activity covers exactly its selected accounts and their owners.'},
      'include_roles': {'type': 'boolean', 'description': "For questions about this member's roles; configured roles only."},
      'include_history': {'type': 'boolean',
          'description': "With account rows, for questions about an account's past owners, clan moves or old names."},
      'role_name': {'type': 'string', 'minLength': 1, 'maxLength': 100,
                    'description': 'Exact guild role name, such as War Co. With clan_codes, intersect its holders with clan home-role holders.'},
      'role_ids': IDS, 'clan_codes': {**CLANS_INPUT, 'description': 'With role_name or role_ids, filter holders by clan home role. Otherwise select configured clan_role.'}, 'role_match': {'type': 'string', 'enum': ['any', 'all']},
      'clan_role': {'type': 'string', 'enum': list(CLAN_ROLES), 'default': 'leadership'},
      'joined_after': DATE, 'joined_before': DATE,
      'accepted_after': DATE, 'accepted_before': DATE,
      'include_accounts':{'type':'boolean'}, 'performance':PERFORMANCE, **ACCOUNT_FILTERS,
      'sort': {'type': 'string', 'enum': ['oldest_first', 'newest_first']}}, member_cohort, required=('basis',),
     contract=CapabilityContract(retained_fields=('cohort_ref',)),
     usage_note='selected: member_ids or read_member_accounts cohort_ref. include_activity reads stored Discord counters; after adds selected-account war activity.'),
)
