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
from ..clan_roles import clan_role_purposes
from .member_context import owner_context
from ..read_cache import stored_read

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


def member_fields(member, identifier):
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
            'home_clan_basis': 'current non-utility Discord clan member roles; not historical acceptance, '
                'extra participation, in-game account location or identification of a main account',
            'roles': [{'role_id': role.id, 'name': role.name,
                       'clan_purposes': clan_role_purposes(role.id)} for role in member.roles
                      if not role.is_default()] if member else []}


async def member_accounts(context, arguments):
    roster_clans, roster_names = {}, {}
    name_selection = {}
    if arguments.get('after') or arguments.get('before'):
        from .war_reads import period
        try:
            period({**arguments, 'before': arguments.get('before')
                    or context.source_message.created_at.isoformat()})
        except ValueError as error:
            return {'error': str(error), 'cause': 'invalid_arguments'}
    if arguments.get('account_names'):
        if any(arguments.get(key) for key in ('member_ids', 'account_tags', 'name', 'cohort_ref')):
            return {'error': 'Which members, accounts or exact name do you want?', 'cause': 'invalid_arguments'}
        from .account_names import resolve_account_names
        name_selection = await resolve_account_names(context, arguments['account_names'])
        roster_names = {tag: row['player_name'] for tag, row in name_selection['accounts'].items()}
        if not roster_names:
            return {'rows': [], 'status': 'needs_input', **{key: value for key, value in name_selection.items()
                    if key != 'accounts'}}
        arguments = {**arguments, 'account_tags': sorted(roster_names)}
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
    if len(selectors) != 1:
        return {'error': 'Which members, accounts or exact name do you want?', 'cause': 'needs_input'}
    identifiers = list(arguments.get('member_ids', []))
    if arguments.get('name'):
        from ..research.member_identity import matching_members, clarification
        members = await matching_members(context, arguments['name'].strip())
        identifiers = [member.id for member in members]
        if len(identifiers) != 1:
            return {'rows': [member_fields(context.guild.get_member(value), value) for value in identifiers],
                    'status': 'needs_input', 'missing': [clarification(arguments['name'], members)],
                    'name_resolution': 'Discord names and linked account names; no unique owner was selected'}
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
    current = await profiles(context, tags)
    owners = {row['player_tag']: identifier for identifier, rows in links.items() for row in rows}
    result = []
    for tag in tags:
        item = current.get(tag, {'status': 'unavailable'})
        data = item.get('data', {})
        identifier = owners.get(tag)
        member = context.guild.get_member(identifier) if identifier else None
        membership = ('links_unavailable' if context.account_links is None else
                      'unlinked' if identifier is None else 'present' if member else 'not_in_guild')
        checks = [{'clan_code': code, 'role_id': CLANS[code].member_role_id,
                   'has_role': (CLANS[code].member_role_id in {role.id for role in member.roles}
                                if member and CLANS[code].member_role_id is not None else None)}
                  for code in sorted(roster_clans.get(tag, ()))]
        result.append({**member_fields(member, identifier),
            'has_elbow_helper_links': identifier in links if context.account_links is not None and identifier else None,
            'membership_status': membership,
            **({'home_role_checks': checks} if checks else {}),
            'player_tag': tag, 'player_name': data.get('name') or roster_names.get(tag),
            'townHallLevel': data.get('townHallLevel'),
            'role': data.get('role'), 'ranked_league': data.get('leagueTier'), 'legacy_league': data.get('league'),
            'clan': data.get('clan'), 'trophies': data.get('trophies'),
            'donations': data.get('donations'),
            'war_stars': data.get('warStars'), 'observed_at': item.get('observed_at'),
            'profile_status': item['status']})
    for identifier in identifiers:
        member = context.guild.get_member(identifier)
        if member:
            context.state.resolved_members[identifier] = member.display_name
        if not links.get(identifier):
            result.append({**member_fields(member, identifier), 'player_tag': None,
                           'has_elbow_helper_links': False if context.account_links is not None else None,
                           'profile_status': 'unlinked' if context.account_links else 'links_unavailable'})
    await require_evidence_access(context)
    matched = [row for row in result if account_matches(row,arguments)]
    history_reads = {}
    if arguments.get('after'):
        from .account_activity import account_activity
        history_reads = await account_activity(context, matched, arguments)
    from .domain_facts import clan_profile
    current_clans = {row.get('clan', {}).get('tag') for row in matched if row.get('clan')}
    return {'rows': sorted(matched, key=lambda row: (row['member_id'] is None,
            row['member_id'] or 0, row.get('player_tag') or '')), 'status': 'resolved',
            'member_count': len(identifiers), 'account_count':None if any(row.get('profile_status')=='links_unavailable' for row in result) else sum(bool(row.get('player_tag')) for row in matched),
            'unknown_profile_count':sum(unknown_account_data(row,arguments) for row in result),
            'unknown_accounts':[row for row in result if unknown_account_data(row,arguments)],
            'count_complete':not any(unknown_account_data(row,arguments) for row in result),
            'clan_facts': [clan_profile(code) for code, clan in CLANS.items() if clan.tag in current_clans],
            **({'home_role_comparisons': [{key: row[key] for key in (
                    'member_id', 'member_name', 'player_tag', 'player_name',
                    'membership_status', 'home_role_checks')} for row in matched],
                'role_comparison_complete': len(matched) == len(roster_clans)} if roster_clans else {}),
            **{key: value for key, value in name_selection.items() if key != 'accounts'},
            **({'history_reads': history_reads} if arguments.get('after') else {}),
            'coverage': ('selected game accounts only; owners do not expand the account selection'
                         if arguments.get('account_tags') else
                         'all bot-linked accounts for selected members; missing profiles remain unknown')}


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
            if row['leadership']['status'] in ('active', 'sleeping') and row['member_id'] is not None)
        retained = [row for row in result if row.get('role') not in ('leader', 'coLeader')
                    and row.get('member_id') not in leadership_owners]
        leadership_filter = {'excluded_accounts': len(result) - len(retained),
            'unverified_accounts': sum(row.get('player_tag') is not None
                and row['leadership']['status'] == 'unknown' for row in retained),
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
            summary['unknown_filter_accounts'] += sum(row['leadership']['status'] == 'unknown'
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
    return {'rows': sorted(result, key=order),
            'count':sum(bool(row.get('player_tag')) for row in result),
            'count_complete':all(not row['unknown_filter_accounts'] and row['cohort_accounts'] is not None for row in summaries),
            **({'leadership_filter': leadership_filter} if leadership_filter is not None else {}),
            'clans': summaries, 'coverage': 'seasonal cohorts and current in-game rosters have distinct bases'}


async def member_cohort(context, arguments):
    basis = arguments['basis']
    deadline = time.monotonic() + max(0, TOOL_TIMEOUT_SECONDS - 5)
    if arguments.get('activity_ref'):
        from .cohort_activity import continue_activity
        return await continue_activity(context, arguments, deadline)
    if arguments.get('role_name') and basis != 'role_holders':
        return {'error': 'Use role_holders to select a named role.', 'cause': 'invalid_arguments'}
    if basis == 'clan_rosters':
        from .cohort_activity import roster_cohort
        return await roster_cohort(context, arguments)
    if arguments.get('exclude_leadership'):
        return {'error': 'exclude_leadership requires basis=clan_rosters.', 'cause': 'invalid_arguments'}
    accepted = {}
    if basis == 'accepted_recruitment':
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
        return {'error': 'Which Discord role holders do you want?', 'cause': 'needs_input'}
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
        rows.append({**member_fields(member, identifier), 'membership_status': 'present' if member else 'not_in_guild', 'selection_status':
                     'unknown_date' if (after or before) and joined is None else 'selected',
                     **({'accepted_at': accepted.get(identifier)}
                     if basis == 'accepted_recruitment' else {})})
    reverse = arguments.get('sort') == 'newest_first'
    sort_field = 'accepted_at' if basis == 'accepted_recruitment' else 'joined_at'
    rows.sort(key=lambda row: (row.get(sort_field) is None,
        (-1 if reverse else 1) * datetime.fromisoformat(row[sort_field]).timestamp() if row.get(sort_field) else 0,
        row['member_id']))
    account_count, unknown_accounts_total = None, 0
    if basis == 'joined_discord' or arguments.get('include_accounts') or arguments.get('include_activity') or any(key in arguments for key in ACCOUNT_FILTERS):
        joined_accounts = await member_accounts(context,{'member_ids':[row['member_id'] for row in rows]}) if rows else {'rows':[]}
        if 'error' in joined_accounts:
            return joined_accounts
        by_member = {}
        for account in joined_accounts['rows']:
            by_member.setdefault(account['member_id'],[]).append(account)
        for row in rows:
            # Owner identity and Discord roles are retained once on the member,
            # not repeated on every alt in this complete cohort snapshot.
            owner_fields = {'member_id', 'member_name', 'nickname', 'joined_at', 'roles',
                            'home_clans', 'participation_clans', 'home_clan_status', 'home_clan_basis',
                            'family_membership_roles', 'has_elbow_helper_links'}
            row['has_elbow_helper_links'] = (any(a.get('player_tag') for a in by_member.get(row['member_id'], []))
                                            if context.account_links is not None else None)
            row['accounts'] = [{key: value for key, value in account.items() if key not in owner_fields}
                               for account in by_member.get(row['member_id'],[]) if account_matches(account,arguments)]
            row['unknown_account_profiles'] = sum(unknown_account_data(account,arguments) for account in by_member.get(row['member_id'],[]))
        unknown_accounts_total = sum(row['unknown_account_profiles'] for row in rows)
        if any(key in arguments for key in ACCOUNT_FILTERS):
            rows = [row for row in rows if any(account.get('player_tag') for account in row['accounts'])]
        account_count = sum(bool(account.get('player_tag')) for row in rows for account in row['accounts'])
    performance = await performance_projection(context,rows,arguments['performance']) if arguments.get('performance') else None
    activity = None
    if arguments.get('include_activity'):
        from .cohort_activity import activity_projection, activity_fields
        activity = await activity_projection(context, rows, arguments, deadline=deadline)
        if 'error' in activity:
            return activity
        activity_fields(rows)
    return {'rows': rows, 'basis': basis, 'coverage': coverage,'member_count':len(rows),'account_count':account_count,
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
 family_tool('read_member_accounts', 'Resolve named game accounts, profiles and dated war/CWL/ranked activity, or join Discord owners.',
     {'member_ids': IDS, 'account_tags': TAGS, 'cohort_ref':COHORT_REF, **ACCOUNT_FILTERS,
      'account_names': {'type': 'array', 'minItems': 1, 'maxItems': 200, 'uniqueItems': True,
          'items': {'type': 'string', 'minLength': 1, 'maxLength': 100},
          'description': 'Game account subjects of stats requests. Search all family rosters and stored names; tolerate spacing/case/typos; return ambiguity by tag.'},
      'after': DATE, 'before': DATE,
      'include_roles': {'type': 'boolean', 'default': False},
      'name': {'type': 'string', 'minLength': 1, 'maxLength': 100,
               'description': 'Explicit Discord-member identity; expands to every linked account of that owner. For game account stats use account_names.'}}, member_accounts,
     usage_note='Stats names use account_names unless explicitly Discord members. after/before reads REG, CWL and ranked history for those tags. home_clans: Discord roles.'),
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
     {'basis': {'type': 'string', 'enum': ['joined_discord', 'role_holders', 'accepted_recruitment', 'clan_rosters']},
      'roster_basis': ROSTER_BASIS,
      'exclude_leadership': EXCLUDE_LEADERSHIP,
      'include_activity': {'type': 'boolean'}, 'after': DATE, 'before': DATE,
      'activity_ref': COHORT_REF,
      'role_name': {'type': 'string', 'minLength': 1, 'maxLength': 100,
                    'description': 'Exact guild role name, such as War Co. With clan_codes, intersect its holders with clan home-role holders.'},
      'role_ids': IDS, 'clan_codes': {**CLANS_INPUT, 'description': 'With role_name or role_ids, filter holders by clan home role. Otherwise select configured clan_role.'}, 'role_match': {'type': 'string', 'enum': ['any', 'all']},
      'clan_role': {'type': 'string', 'enum': list(CLAN_ROLES), 'default': 'leadership'},
      'joined_after': DATE, 'joined_before': DATE,
      'accepted_after': DATE, 'accepted_before': DATE,
      'include_accounts':{'type':'boolean'}, 'performance':PERFORMANCE, **ACCOUNT_FILTERS,
      'sort': {'type': 'string', 'enum': ['oldest_first', 'newest_first']}}, member_cohort, required=('basis',),
     contract=CapabilityContract(entity_fields=(('activity_ref', 'family_report'),), retained_fields=('activity_ref',)),
     usage_note='role_name or role_ids + clan_codes: intersect home roles. include_activity + after: selected members, linked accounts, war and chat metadata.'),
)
