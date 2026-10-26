"""Complete roster cohorts with account donations, war evidence and chat metadata."""
import asyncio
from datetime import datetime
import json
import time

import discord

from elbow_helper.discord.message_search import DiscordMessageHistoryError
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from .war_reads import selected_wars, period
from .core import FamilyReport
from ..reports.tools import authorize_report
from ..access import accessible_message_channel, AgentAccessLost
from ..engine.budgets import TOOL_TIMEOUT_SECONDS
from ..research.history import searchable_channel_ids


async def roster_cohort(context, arguments):
    from .identity_reads import clan_rosters, member_fields, DEFAULT_ROSTER_BASIS
    # Leave time to retain and project the full roster after cancellable reads.
    deadline = time.monotonic() + max(0, TOOL_TIMEOUT_SECONDS - 5)
    if arguments.get('activity_ref'):
        return await continue_activity(context, arguments, deadline)
    roster = await clan_rosters(context, {**arguments,
        'basis': arguments.get('roster_basis', DEFAULT_ROSTER_BASIS)})
    if 'error' in roster:
        return roster
    tags = sorted({row['player_tag'] for row in roster['rows'] if row.get('player_tag')})
    links = await asyncio.to_thread(context.account_links.get_links_by_tags, tags) if context.account_links else {}
    groups = {}
    for account in roster['rows']:
        tag = account.get('player_tag')
        owner = (links.get(tag) or {}).get('discord_user_id')
        member = context.guild.get_member(owner) if owner else None
        key = ('member', owner) if owner else ('account', tag or account['clan_code'])
        row = groups.setdefault(key, {**member_fields(member, owner), 'accounts': [],
            'membership_status': 'links_unavailable' if context.account_links is None else
                'unlinked' if owner is None else 'present' if member else 'not_in_guild'})
        row['accounts'].append(account)
    rows = list(groups.values())
    activity = await activity_projection(context, rows, arguments, deadline=deadline) if arguments.get('include_activity') else None
    if activity and 'error' in activity:
        return activity
    if activity:
        activity_fields(rows)
    clan_activity = []
    if activity:
        for code in sorted({a['clan_code'] for row in rows for a in row['accounts']}):
            selected = [(row['member_id'], a['war_activity']) for row in rows
                        for a in row['accounts'] if a['clan_code'] == code]
            attacking = [(owner, value) for owner, value in selected
                         if (value['attacks_used'] or 0) > 0]
            clan_activity.append({'clan_code': code, 'current_accounts': len(selected),
                'accounts_with_observed_attacks': len(attacking),
                'linked_owners_with_observed_attacks': len({owner for owner, _ in attacking if owner}),
                'accounts_observed_without_attacks': sum(v['status'] == 'observed'
                    and v['totals_complete'] and v['attacks_used'] == 0 for _, v in selected),
                'accounts_with_incomplete_activity': sum(v['status'] == 'source_unavailable'
                    or v['status'] == 'observed' and not v['totals_complete'] for _, v in selected),
                'accounts_without_stored_participation': sum(v['status'] == 'no_stored_participation'
                    for _, v in selected)})
    rows.sort(key=lambda row: (row['member_id'] is None, row['member_id'] or 0,
                               row['accounts'][0].get('player_tag') or ''))
    return {'rows': rows, 'basis': 'clan_rosters', 'account_count': len(tags),
            'member_count': sum(row['member_id'] is not None for row in rows),
            'unlinked_account_count': sum(bool(a.get('player_tag')) for r in rows if r['member_id'] is None for a in r['accounts']),
            'count_complete': roster['count_complete'],
            **({'leadership_filter': roster['leadership_filter']} if 'leadership_filter' in roster else {}),
            'clans': [{key: clan[key] for key in ('clan_code', 'name', 'tag', 'basis', 'status',
                        'cohort_accounts', 'included_accounts', 'excluded_accounts', 'unknown_filter_accounts',
                        'in_game_members', 'in_game_capacity', 'open_in_game_slots') if key in clan}
                      for clan in roster['clans']],
            'activity_coverage': activity,
            **({'clan_activity': clan_activity} if activity else {}),
            'coverage': 'Every selected roster account, grouped by current bot-linked owner. Unlinked and departed owners remain explicit.'}


async def continue_activity(context, arguments, deadline):
    identifier = arguments['activity_ref']
    report = context.state.reports.get(identifier)
    if (set(arguments) - {'basis', 'activity_ref'} or not isinstance(report, FamilyReport)
            or report.guild_id != context.guild.id or report.family != 'read_member_cohort'
            or not await authorize_report(context, identifier)):
        return {'error': 'That cohort is no longer available here. Read it again.', 'cause': 'report_unavailable'}
    snapshot = json.loads(report.payload)
    summary = snapshot['summary']
    coverage = summary.get('activity_coverage')
    if (arguments.get('basis') != summary.get('basis')
            or summary.get('requester_id') != context.member.id
            or not isinstance(coverage, dict)):
        return {'error': 'Read a new cohort for this request.', 'cause': 'report_unavailable'}
    # Enrich a copy, leaving the previous immutable snapshot and selection intact.
    chat = await chat_projection(context, snapshot['rows'], coverage['period'], deadline=deadline,
                                 progress=coverage['chat_coverage'])
    summary['activity_coverage'] = {**coverage, 'chat_coverage': chat}
    activity_fields(snapshot['rows'])
    return {**summary, 'rows': snapshot['rows']}


def activity_fields(rows):
    """Typed member measures also form a compact complete writer table."""
    for row in rows:
        row.update(account_tags=[a['player_tag'] for a in row['accounts'] if a.get('player_tag')],
                   account_names=[a.get('player_name') for a in row['accounts']],
                   clan_codes=sorted({a['clan_code'] for a in row['accounts'] if a.get('clan_code')}),
                   war_status=row['war_activity']['status'],
                   wars_observed=row['war_activity']['wars_observed'],
                   attacks_used=row['war_activity']['attacks_used'],
                   attacks_expected=row['war_activity'].get('attacks_expected'),
                   missed=row['war_activity'].get('missed'),
                   attack_completion_percent=row['war_activity'].get('attack_completion_percent'),
                   war_totals_complete=row['war_activity']['totals_complete'],
                   chat_status=row['chat_activity']['status'],
                   latest_chat_at=row['chat_activity']['latest_observed_at'])


def war_measures(observed, *, unavailable=False):
    """Keep the actual opportunity denominator, including mixed war types."""
    complete = bool(observed) and all(r['totals_complete'] for r in observed)
    used = sum(r['attacks_used'] for r in observed) if observed else None
    expected = sum(r['attacks_expected'] for r in observed) if complete else None
    return {
        'status': 'source_unavailable' if unavailable else 'observed' if observed else 'no_stored_participation',
        'wars_observed': None if unavailable else len({war for r in observed for war in r['war_ids']}),
        'attacks_used': used, 'attacks_expected': expected,
        'missed': expected - used if expected is not None else None,
        'attack_completion_percent': round(100 * used / expected, 2) if expected else None,
        'unconfirmed_opportunities': sum(r['unconfirmed_opportunities'] for r in observed) if observed else None,
        'totals_complete': complete}


async def activity_projection(context, rows, arguments, *, deadline=None):
    selection = {key: arguments[key] for key in ('clan_codes', 'after', 'before') if key in arguments}
    if not selection.get('after'):
        return {'error': 'Which season, wars or date window do you want?', 'cause': 'needs_input', 'missing_fields': ['after']}
    selection.setdefault('before', context.source_message.created_at.isoformat())
    try:
        period(selection)
    except (ValueError, TypeError):
        return {'error': 'Choose a complete date window with its end after its start.', 'cause': 'invalid_arguments'}
    wars = await selected_wars(context, selection, 'both')
    measured = {r['player_tag']: r for r in aggregate_attendance(wars.get('rows', []), {}, 'account')}
    for row in rows:
        for account in row['accounts']:
            observation = measured.get(account.get('player_tag'))
            account['war_activity'] = war_measures([observation] if observation else [],
                                                  unavailable='error' in wars)
    for row in rows:
        accounts = row['accounts']
        donation_values = [account.get('donations') for account in accounts]
        row['donations'] = sum(donation_values) if all(type(v) is int for v in donation_values) else None
        tag_set = {a['player_tag'] for a in accounts if a.get('player_tag')}
        observed = [measured[tag] for tag in tag_set if tag in measured]
        row['war_activity'] = war_measures(observed, unavailable='error' in wars)
    chat = await chat_projection(context, rows, selection, deadline=deadline)
    return {'period': selection, 'war_coverage': wars.get('coverage'),
            'war_source_status': wars.get('cause') if 'error' in wars else 'available',
            'donation_basis': 'Current Clash season counters; not donations within the requested chat/war window.',
            'war_measure_basis': 'Account war_activity measures that account; row totals combine only the selected '
                'currently rostered accounts for that owner, not every account they own. Historical wars may '
                'have been fought in another selected clan; current location does not establish where attacks occurred.',
            'completion_basis': 'Attack completion is attacks used / expected in observed account-war opportunities, '
                'not an overall activity estimate. Regular and CWL wars may allow different attack counts; '
                'war count is not the denominator. Missing or incomplete opportunities remain unknown.',
            'chat_coverage': chat, 'historical_collection_complete': False,
            'absence_meaning': 'No stored war participation or no observed chat message does not prove inactivity. Unlinked accounts cannot establish a member chat history.'}


async def chat_projection(context, rows, selection, *, deadline=None, progress=None):
    identifiers = {row['member_id'] for row in rows if row.get('member_id')}
    channels = (tuple(progress['channel_ids']) if progress is not None else
                await searchable_channel_ids(context, limit=None))
    if progress is not None:
        for channel_id in channels:
            if await accessible_message_channel(context, channel_id) is None:
                raise AgentAccessLost('Retained activity channel is no longer available')
    context.state.source_channels.update(channels)
    after, before = (datetime.fromisoformat(selection[key]) for key in ('after', 'before'))
    lower, upper = discord.utils.time_snowflake(after) - 1, discord.utils.time_snowflake(before)
    cursors = dict(progress['channel_cursors']) if progress is not None else {str(c): upper for c in channels}
    failed = set(progress.get('unavailable_channel_ids', ())) if progress is not None else set()
    results = {row['member_id']: row['chat_activity'] for row in rows
               if row.get('member_id') and row.get('chat_activity', {}).get('status') == 'observed'}
    semaphore = asyncio.Semaphore(8)

    async def read(channel_id):
        async with semaphore:
            key = str(channel_id)
            try:
                if context.message_search is None:
                    raise DiscordMessageHistoryError('Channel history unavailable')
                while cursors[key] is not None:
                    bound = cursors[key]
                    page = await context.message_search.history_page(channel_id=channel_id,
                        after_id=lower, before_id=bound, limit=100)
                    if any(m.channel_id != channel_id or not lower < m.message_id < bound for m in page.messages):
                        raise DiscordMessageHistoryError('Chat metadata outside selected scope')
                    for message in page.messages:
                        identifier = message.author_id
                        if identifier not in identifiers or message.message_id <= results.get(identifier, {}).get('latest_message_id', 0):
                            continue
                        results[identifier] = {'status': 'observed', 'latest_observed_at': message.timestamp,
                            'latest_message_id': message.message_id,
                            'source': f'https://discord.com/channels/{context.guild.id}/{channel_id}/{message.message_id}'}
                    following = page.next_before_id
                    if following is not None and not lower < following < bound:
                        raise DiscordMessageHistoryError('Channel history did not advance')
                    if following is None and not page.reached_window_start:
                        raise DiscordMessageHistoryError('Channel history is incomplete')
                    cursors[key] = following
            except (DiscordMessageHistoryError, discord.HTTPException, OSError, ValueError):
                failed.add(channel_id)
                cursors[key] = None

    tasks = {asyncio.create_task(read(c)): c for c in channels if cursors[str(c)] is not None}
    if tasks:
        try:
            remaining = deadline - time.monotonic() if deadline is not None else TOOL_TIMEOUT_SECONDS - 5
            await asyncio.wait(tasks, timeout=max(0, remaining))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for task, outcome in zip(tasks, outcomes):
                if isinstance(outcome, Exception):
                    failed.add(tasks[task])
                    cursors[str(tasks[task])] = None
    complete = all(value is None for value in cursors.values())
    for row in rows:
        row['chat_activity'] = results.get(row['member_id'], {
            'status': ('unlinked' if not row['member_id'] else 'not_checked_timeout' if not complete
                       else 'source_unavailable' if failed else 'no_observed_messages'),
            'latest_observed_at': None})
    return {'channel_count': len(channels), 'channel_ids': list(channels), 'channel_cursors': cursors,
            'unavailable_channel_ids': sorted(failed), 'unavailable_channels': len(failed),
            'channels_not_checked': sum(value is not None for value in cursors.values()),
            'members_checked': len(identifiers) if complete else len(results),
            'members_not_checked': 0 if complete else len(identifiers) - len(results),
            'source_reads_complete': complete, 'counts_complete': False,
            'basis': 'Recent authorized channel history metadata across the retained date window; message text is not inspected.'}
