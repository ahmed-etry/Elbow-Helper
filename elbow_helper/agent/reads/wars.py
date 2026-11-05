"""Typed selections and thin projections over collected war reads; no network clients."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Literal

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from elbow_helper.features.wars.record import board_payload, record_fresh
from . import record_wars
from .member_context import owner_context
from ..read_cache import source_revision, stored_read
from .war_measures import aggregate_performance, clan_participation, participation_status


@dataclass(frozen=True)
class WarRequest:
    war_type: Literal['REG', 'CWL', 'both']
    clan_codes: tuple[str, ...]
    after_ts: int | None
    before_ts: int | None
    season: str | None
    war_ids: tuple[str, ...]
    latest_completed: int | None

    def regular_arguments(self):
        return {'clan_codes': list(self.clan_codes), 'after_ts': self.after_ts, 'before_ts': self.before_ts,
                'war_ids': self.war_ids,
                **({'latest_completed': self.latest_completed} if self.latest_completed is not None else {})}


def period(arguments):
    values = []
    for field in ('after', 'before'):
        value = arguments.get(field)
        date = datetime.fromisoformat(value) if value else None
        if date is not None and date.utcoffset() is None:
            raise ValueError('Choose timezone-aware dates.')
        values.append(int(date.timestamp()) if date else None)
    after, before = values
    if (after is None) != (before is None) or after is not None and before <= after:
        raise ValueError('Choose a complete date window with its end after its start.')
    return after, before


def single_selection(context, arguments):
    """Arguments with the window closed, and its bounds, once exactly one selector is used."""
    if arguments.get('after') and not arguments.get('before'):
        arguments = {**arguments, 'before': context.source_message.created_at.isoformat()}
    after, before = period(arguments)
    selectors = [bool(arguments.get(key)) for key in ('season', 'season_keys', 'war_ids', 'latest_completed', 'history_limit')]
    if sum(selectors) + (after is not None) > 1:
        raise ValueError('Choose one season selection, war selection or date window.')
    return arguments, after, before


async def record_selection(context, selection):
    """Regular wars from the family record's war reads; league wars from the CWL queries over the same record."""
    dependencies = getattr(context, 'read_dependencies', None)
    if dependencies is not None:
        # As stored reads do: taken before the read, and only while reads are cached.
        reads = record_wars.war_reads(context)
        revision = (await asyncio.to_thread(source_revision, reads)
                    if getattr(context, 'read_cache', None) is not None else None)
        dependencies.observe(context, reads, revision)
    try:
        request = WarRequest(selection['war_type'], tuple(selection['clan_codes']),
                             selection.get('after_ts'), selection.get('before_ts'), selection.get('season'),
                             tuple(selection.get('war_ids', ())), selection.get('latest_completed'))
        regular = await record_wars.regular_wars(context, **request.regular_arguments())
    except record_wars.FAILURES:
        return dict(record_wars.UNAVAILABLE)
    if selection['war_type'] == 'REG':
        return regular
    league = await stored_read(context, context.cwl_queries, 'attendance', **{**selection, 'war_type': 'CWL'})
    return record_wars.combine(regular, league, selection.get('latest_completed'))


async def selected_wars(context, arguments, war_type):
    # Regular wars need the record's war reads; league wars need the CWL queries.
    if (war_type != 'CWL' and record_wars.war_reads(context) is None
            or war_type != 'REG' and context.cwl_queries is None):
        return {'error': 'Stored war evidence is not available.', 'cause': 'missing_setup'}
    try:
        arguments, after, before = single_selection(context, arguments)
        if arguments.get('season_keys') and war_type != 'CWL' or war_type == 'both' and arguments.get('season'):
            sources = []
            for season in arguments.get('season_keys', [arguments.get('season')]):
                for kind in ('REG', 'CWL') if war_type == 'both' else (war_type,):
                    source = await selected_wars(context, {**{key: value for key, value in arguments.items()
                        if key != 'season_keys'}, 'season': season}, kind)
                    if 'error' in source:
                        return source
                    sources.append(source)
            return {'rows': [row for source in sources for row in source['rows']],
                    'war_ids': sorted({value for source in sources for value in source['war_ids']}),
                    'source_coverage': [source['coverage'] for source in sources],
                    'coverage': {'query_complete': True, 'historical_collection_complete': False,
                        'exact_ranking': all(source['coverage'].get('exact_ranking', False) for source in sources)}}
        season = arguments.get('season')
        for key in arguments.get('season_keys', []) or ([season] if season else []):
            if not isinstance(key, str) or re.fullmatch(SEASON_KEY, key) is None:
                raise ValueError('Choose a season in YYYY-MM form, or a CWL season key a read returned.')
            month = key[:7]
            start = datetime.fromisoformat(month + '-01').replace(tzinfo=timezone.utc)
            if war_type == 'REG':
                if key != month:
                    raise ValueError('Season keys other than YYYY-MM select CWL wars only.')
                end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
                after, before, season = int(start.timestamp()), int(end.timestamp()), None
        if not (after is not None or season or arguments.get('season_keys') or arguments.get('history_limit')
                or arguments.get('war_ids') or arguments.get('latest_completed')):
            return {'error': 'Which season, wars or date window do you want?', 'cause': 'invalid_arguments',
                    'missing_fields': ['season', 'season_keys', 'after', 'war_ids', 'latest_completed', 'history_limit']}
        selection = dict(clan_codes=arguments.get('clan_codes', list(CLANS)), war_type=war_type,
            after_ts=after, before_ts=before, season=season,
            war_ids=arguments.get('war_ids', ()),
            **{key: arguments[key] for key in ('season_keys', 'history_limit') if arguments.get(key)},
            **({'latest_completed': arguments['latest_completed']} if arguments.get('latest_completed') else {}))
        if war_type == 'CWL':
            return await stored_read(context, context.cwl_queries, 'attendance', **selection)
        # Recent seasons are a league selection, refused for regular wars as league history refuses them.
        if arguments.get('history_limit'):
            raise ValueError('Invalid CWL history limit')
        return await record_selection(context, selection)
    except ValueError as error:
        return {'error': str(error), 'cause': 'invalid_arguments'}
    except (OSError, sqlite3.Error):
        return {'error': 'Stored war evidence is unavailable.', 'cause': 'source_unavailable'}


async def attendance(context, arguments, *, separate_seasons=False):
    if arguments.get('cohort_ref'):
        from .projections import cohort_selection
        selected = await cohort_selection(context,arguments['cohort_ref'],'account_tags')
        if 'error' in selected:
            return selected
        arguments = {**arguments,**selected}
        if arguments.get('unit') == 'member':
            snapshot = json.loads(context.state.reports[arguments['cohort_ref']].payload)['rows']
            members = sorted({row.get('member_id') or row.get('applicant_member_id') for row in snapshot
                              if row.get('member_id') or row.get('applicant_member_id')})
            if members:
                arguments = {**arguments, 'member_ids': members}
    source = await selected_wars(context, arguments, arguments['war_type'])
    if 'error' in source:
        return source
    tags = sorted({row['player_tag'] for row in source['rows']})
    linked = await asyncio.to_thread(context.account_links.get_links_by_tags, tags) if context.account_links else {}
    owners = {tag: value['discord_user_id'] for tag, value in linked.items() if value}
    rows = [row for row in source['rows'] if (
        not arguments.get('account_tags') or row['player_tag'] in arguments['account_tags']) and (
        not arguments.get('member_ids') or owners.get(row['player_tag']) in arguments['member_ids'])]
    if separate_seasons:
        groups = defaultdict(list)
        for row in rows:
            groups[(row['season'], row['clan_code'])].append(row)
        result = [dict(row, season=season, clan_code=code) for (season, code), observations in groups.items()
                  for row in aggregate_attendance(observations, owners, arguments.get('unit', 'account'))]
    else:
        result = aggregate_attendance(rows, owners, arguments.get('unit', 'account'))
    unit = arguments.get('unit', 'account')
    selected_tags = set(arguments.get('account_tags', []))
    if arguments.get('member_ids') and not arguments.get('account_tags') and context.account_links:
        member_links = await asyncio.to_thread(context.account_links.get_links_for_members, arguments['member_ids'])
        selected_tags.update(link['player_tag'] for links in member_links.values() for link in links)
    if selected_tags and context.account_links:
        selected_links = await asyncio.to_thread(context.account_links.get_links_by_tags, sorted(selected_tags))
        owners.update({tag: value['discord_user_id'] for tag, value in selected_links.items() if value})
    observed_tags = {row['player_tag'] for row in rows}
    if unit == 'account':
        missing = [(tag, None) for tag in sorted(selected_tags - observed_tags)]
    else:
        observed_members = {owners.get(tag) for tag in observed_tags}
        requested_members = set(arguments.get('member_ids', [])) | {owners[tag] for tag in selected_tags if tag in owners}
        missing = [(None, member) for member in sorted(requested_members - observed_members)]
        missing.extend((tag, None) for tag in sorted(selected_tags - observed_tags) if tag not in owners)
    # With a stored lineup for every checked war, an account in none of them is a known zero.
    # A war without a lineup, or a selection that checked no war, leaves it unknown.
    absent_known = 'wars_without_rosters' in source and bool(source.get('war_ids')) and not source['wars_without_rosters']
    for tag, member in missing:
        result.append({'member_id': member, 'player_tag': tag, 'player_name': None,
            'account_tags': [tag] if tag else sorted(t for t in selected_tags if owners.get(t) == member),
            'war_ids': [], 'attacks_used': 0 if absent_known else None,
            'attacks_expected': 0 if absent_known else None, 'missed': 0 if absent_known else None,
            'miss_rate': None, 'unconfirmed_opportunities': 0, 'unit': 'account' if tag else unit,
            'missing_link': member is None and unit == 'member', 'totals_complete': absent_known,
            'observation_status': 'not_in_checked_lineups' if absent_known else 'no_stored_participation'})
    opportunities = defaultdict(list)
    for item in rows:
        owner = owners.get(item['player_tag'])
        key = ('member', owner) if unit == 'member' and owner else ('account', item['player_tag'])
        if separate_seasons:
            key = (item['season'], item['clan_code'], key)
        opportunities[key].append(item)
    for row in result:
        if unit == 'account':
            row['member_id'] = owners.get(row['player_tag'])
        key = ('account', row['player_tag']) if row['player_tag'] else ('member', row['member_id'])
        if separate_seasons:
            key = (row.get('season'), row.get('clan_code'), key)
        row['participation_status'] = ('not_in_checked_lineups' if row.get('observation_status') == 'not_in_checked_lineups'
                                       else participation_status(opportunities[key]))
        row['attack_performance'] = {field: value for field, value in
            aggregate_performance(opportunities[key]).items() if field != 'accounts'}
        member = context.guild.get_member(row['member_id']) if row['member_id'] else None
        row.update(owner_context(member, row['member_id']), account_purpose=None)
    measure = arguments.get('sort', 'missed')
    result.sort(key=lambda row: (row.get(measure) is None, -(row.get(measure) or 0),
        -(row['attacks_expected'] or 0), row['member_id'] or 0, row['player_tag'] or ''))
    population = len(result)
    if arguments.get('top_n'):
        result = result[:arguments['top_n']]
    clan_totals = []
    periods = [(season, code) for season in source.get('component_seasons', [arguments.get('season')])
               for code in arguments.get('clan_codes', sorted({r['clan_code'] for r in rows if r.get('clan_code')}))]
    for season, code in periods:
        observations = [row for row in rows if row.get('clan_code') == code
                        and (season is None or row.get('season') == season)]
        selected = [war for war in source.get('war_aliases', []) if war['clan_code'] == code
                    and (season is None or war.get('season') == season)]
        empty = set(source.get('wars_without_rosters', []))
        confirmed = (bool(observations) and all(row.get('missed') is not None for row in observations)
                     and not any(war['war_id'] in empty for war in selected))
        war_count = len(selected) if 'war_aliases' in source else len({row['war_id'] for row in observations})
        clan_totals.append({'clan_code': code, 'season': season, 'wars': war_count,
            'accounts': len({row['player_tag'] for row in observations}),
            'attacks_used': sum(row.get('attacks_used') or 0 for row in observations) if confirmed else None,
            'attacks_expected': sum(row.get('attacks_expected') or 0 for row in observations) if confirmed else None,
            'missed': sum(row['missed'] for row in observations) if confirmed else None,
            'totals_complete': confirmed,
            **({'season_complete': confirmed and war_count >= 7} if separate_seasons and season else {})})
    from .member_context import account_owner_profiles
    member_profiles = account_owner_profiles(result) if unit == 'account' else None
    return {**source, 'rows': result, 'clan_totals': clan_totals,
            **({'members': member_profiles} if member_profiles is not None else {}),
            'ranked_population': population, 'opportunity_unit': 'account-war',
            'clan_participation': clan_participation(rows, owners, context.guild,
                arguments.get('clan_codes', sorted({r['clan_code'] for r in rows if r.get('clan_code')}))),
            'participation_meanings': {
                'warring': 'Joined a stored war and attacked at least once in the selected period.',
                'joined_without_attacking': 'Joined stored wars but used no attacks; confirmed final participation only.',
                'unknown': 'Attack participation is incomplete or not stored.',
                'not_in_checked_lineups': 'In none of the checked wars, all of which have stored lineups; zero opportunities.',
                'counting': 'Distinct members within each clan; unlinked accounts counted separately. A member appearing in several clans is counted in each, so clan counts cannot be summed as unique people.'},
            'ownership_basis': 'current bot links; historical ownership unknown',
            'interpretation': 'Participation describes selected account-war opportunities, not a member reliability verdict. '
                'Account purpose and expected participation remain unknown unless established separately.',
            'ordering': measure + ' descending, expected descending, identity ascending'}


async def current_wars(context, codes):
    """Latest checks in the compatibility shape; unavailable checks have no state."""
    reads, items = record_wars.war_reads(context), []
    for code in codes:
        tag = CLANS[code].tag
        current = await reads.current(tag, lineups=True)
        signal = current['coverage']['signals'][0]
        if not record_fresh(current) or current['state'] != 'notInWar' and current['war'] is None:
            status = signal['status'] if signal['status'] not in ('observed', 'partial') else 'unavailable'
            items.append({'tag': tag, 'status': status, 'observed_at': record_wars.iso(signal['last_good_at'])})
            continue
        war = current['war']
        data = board_payload(war) if war is not None else {'state': 'notInWar'}
        used = {member['player_tag']: member['attacks_used'] for member in (war or {}).get('clan', {}).get('members', ())
                if member['attacks_used'] is not None}
        # Counts and lineup come from the same snapshot; ended does not by itself prove final counts.
        final = {row['player_tag']: row['missed'] for row in current['participation']}
        items.append({'tag': tag, 'status': 'ok', 'data': data, 'attacks_used': used, 'missed': final,
                      'observed_at': record_wars.iso(current['checked_at'])})
    return {'items': items}


async def current_war(context, arguments):
    codes = arguments.get('clan_codes', list(CLANS))
    if record_wars.war_reads(context) is None:
        return {'error': 'Stored war evidence is not available.', 'cause': 'missing_setup'}
    try:
        source = await current_wars(context, codes)
    except record_wars.FAILURES:
        return dict(record_wars.UNAVAILABLE)
    rows = []
    by_tag = {item['tag']: item for item in source['items']}
    for code in codes:
        item = by_tag.get(CLANS[code].tag, {'status': 'unavailable'})
        data = item.get('data', {})
        state = data.get('state')
        members = []
        for row in data.get('clan', {}).get('members', []):
            used = item.get('attacks_used', {}).get(row['tag'], len(row.get('attacks', [])))
            expected = data.get('attacksPerMember')
            remaining = max(0,expected-used) if type(expected) is int and expected>0 else None
            if arguments.get('account_tags') and row['tag'] not in arguments['account_tags']:
                continue
            members.append({'player_tag': row['tag'], 'player_name': row.get('name'),
                            'map_position': row.get('mapPosition'),
                            **({'attacks': row.get('attacks', [])} if arguments.get('include_attacks') else {}),
                            'attacks_used': used, 'unused_attacks': remaining,
                            'missed': item.get('missed', {}).get(row['tag']) if 'missed' in item
                                      else remaining if state == 'warEnded' else None})
        rows.append({'clan_code': code, 'status': item['status'], 'state': state,
                     'start_at': data.get('startTime'), 'end_at': data.get('endTime'),
                     'team_size': data.get('teamSize'), 'opponent': data.get('opponent', {}).get('name'),
                     'score': {side: {key: data.get(side, {}).get(key) for key in ('attacks', 'stars', 'destructionPercentage')}
                               for side in ('clan', 'opponent')},
                     'members': sorted(members, key=lambda row: (row['map_position'] is None,
                         row['map_position'] or 0, row['player_tag'])) if arguments.get('include_roster', True) else [],
                     'observed_at': item.get('observed_at')})
    return {'rows': rows, 'coverage': 'latest collected current-war check; a failed or overdue check gives no war; '
                                      'unused attacks are not misses before war ends'}


async def performance_projection(context, rows, selection, *, account_tags=None):
    ids = sorted({row.get('member_id') or row.get('applicant_member_id') for row in rows
                  if row.get('member_id') or row.get('applicant_member_id')})
    if not ids:
        return {'coverage':'No resolved member IDs for a performance join.'}
    result = await selected_wars(context, selection, selection['war_type'])
    if 'error' in result:
        return {'error':result['error'],'cause':result.get('cause','source_unavailable')}
    tags = sorted({row['player_tag'] for row in result['rows']})
    linked = await asyncio.to_thread(context.account_links.get_links_by_tags, tags) if context.account_links else {}
    owners = {tag: value['discord_user_id'] for tag, value in linked.items() if value}
    selected = [row for row in result['rows'] if owners.get(row['player_tag']) in ids
                and (account_tags is None or row['player_tag'] in account_tags)]
    by_member = {row['member_id']: row for row in aggregate_attendance(selected, owners, 'member')}
    by_member_type = defaultdict(list)
    for opportunity in selected:
        by_member_type[(owners[opportunity['player_tag']], opportunity['war_type'])].append(opportunity)
    for row in rows:
        identifier = row.get('member_id') or row.get('applicant_member_id')
        row['war_attendance'] = by_member.get(identifier)
        performance = {}
        for kind in ('REG', 'CWL'):
            metrics = aggregate_performance(by_member_type[(identifier, kind)])
            performance[kind] = {key: metrics[key] for key in (
                'wars', 'account_war_opportunities', 'observed_attacks', 'average_stars',
                'triple_rate_percent', 'average_destruction_percent', 'measurement_status', 'accounts')}
        row['attack_performance'] = performance
    return {**{key:value for key,value in result.items() if key!='rows'}, 'resolved_selection': selection}


