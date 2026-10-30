"""Complete roster cohorts with account donations, war evidence and stored Discord activity."""
import asyncio
from elbow_helper.features.clan_health.attendance import aggregate_attendance
from .war_reads import selected_wars, period


async def roster_cohort(context, arguments):
    from .identity_reads import clan_rosters, member_fields, DEFAULT_ROSTER_BASIS
    from .member_context import PROFILE_ROLE_GROUPS
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
    activity = await activity_projection(context, rows, arguments) if arguments.get('include_activity') else None
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
                    for _, v in selected),
                **({'accounts_not_checked': len(selected)}
                   if activity['war_source_status'] == 'not_checked' else {})})
    rows.sort(key=lambda row: (row['member_id'] is None, row['member_id'] or 0,
                               row['accounts'][0].get('player_tag') or ''))
    return {'rows': rows, 'basis': 'clan_rosters',
            **({'configured_role_groups': list(PROFILE_ROLE_GROUPS)} if arguments.get('include_roles') else {}),
            'account_count': len(tags),
            'member_count': sum(row['member_id'] is not None for row in rows),
            'unlinked_account_count': sum(bool(a.get('player_tag')) for r in rows if r['member_id'] is None for a in r['accounts']),
            'count_complete': roster['count_complete'],
            **({'leadership_filter': roster['leadership_filter']} if 'leadership_filter' in roster else {}),
            'clans': [{key: clan[key] for key in ('clan_code', 'name', 'tag', 'basis', 'status',
                        'observed_at',
                        'cohort_accounts', 'included_accounts', 'excluded_accounts', 'unknown_filter_accounts',
                        'in_game_members', 'in_game_capacity', 'open_in_game_slots') if key in clan}
                      for clan in roster['clans']],
            'activity_coverage': activity,
            **({'clan_activity': clan_activity} if activity else {}),
            'coverage': 'Every selected roster account, grouped by current bot-linked owner. Unlinked and departed owners remain explicit.'}


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
                   discord_activity_status=row['discord_activity']['status'],
                   last_active_day=row['discord_activity']['last_active_day'],
                   active_streak_days=row['discord_activity']['active_streak_days'],
                   lifetime_messages=row['discord_activity']['lifetime_messages'])


def war_measures(observed, *, unavailable=False, not_checked=False):
    """Keep the actual opportunity denominator, including mixed war types."""
    complete = bool(observed) and all(r['totals_complete'] for r in observed)
    used = sum(r['attacks_used'] for r in observed) if observed else None
    expected = sum(r['attacks_expected'] for r in observed) if complete else None
    return {
        'status': 'not_checked' if not_checked else
                  'source_unavailable' if unavailable else 'observed' if observed else 'no_stored_participation',
        'wars_observed': None if unavailable or not_checked else len({war for r in observed for war in r['war_ids']}),
        'attacks_used': used, 'attacks_expected': expected,
        'missed': expected - used if expected is not None else None,
        'attack_completion_percent': round(100 * used / expected, 2) if expected else None,
        'unconfirmed_opportunities': sum(r['unconfirmed_opportunities'] for r in observed) if observed else None,
        'totals_complete': complete}


async def activity_projection(context, rows, arguments):
    selection = {key: arguments[key] for key in ('clan_codes', 'after', 'before') if key in arguments}
    not_checked = not selection.get('after')
    if not_checked:
        wars = {'coverage': {'status': 'not_checked', 'reason': 'no_period',
            'message': 'Game war activity was not checked because no period was supplied.'}}
    else:
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
                                                  unavailable='error' in wars, not_checked=not_checked)
    for row in rows:
        accounts = row['accounts']
        donation_values = [account.get('donations') for account in accounts]
        row['donations'] = sum(donation_values) if all(type(v) is int for v in donation_values) else None
        tag_set = {a['player_tag'] for a in accounts if a.get('player_tag')}
        observed = [measured[tag] for tag in tag_set if tag in measured]
        row['war_activity'] = war_measures(observed, unavailable='error' in wars, not_checked=not_checked)
    activity = await discord_activity_projection(context, rows)
    return {'period': None if not_checked else selection, 'war_coverage': wars.get('coverage'),
            'war_source_status': 'not_checked' if not_checked else
                wars.get('cause') if 'error' in wars else 'available',
            'donation_basis': 'Current Clash season counters; not donations within the requested war window.',
            'war_measure_basis': 'Account war_activity measures that account; row totals combine only the selected '
                'accounts for that owner, not every account they own. Historical wars may '
                'have been fought in another selected clan; current location does not establish where attacks occurred.',
            'completion_basis': 'Attack completion is attacks used / expected in observed account-war opportunities, '
                'not an overall activity estimate. Regular and CWL wars may allow different attack counts; '
                'war count is not the denominator. Missing or incomplete opportunities remain unknown.',
            'discord_activity_coverage': activity, 'historical_collection_complete': False,
            'absence_meaning': 'No stored war participation or no posting record does not prove inactivity. Unlinked accounts cannot establish Discord activity.'}


async def discord_activity_projection(context, rows):
    """Use stored all-channel posting counters without reading channel history."""
    identifiers = sorted({row['member_id'] for row in rows if row.get('member_id')})
    records, available = {}, context.achievement_queries is not None
    if identifiers and available:
        try:
            records = await asyncio.to_thread(context.achievement_queries.member_activity, identifiers)
        except (RuntimeError, OSError):
            available = False
    for row in rows:
        row['discord_activity'] = records.get(row.get('member_id'), {
            'status': 'unlinked' if row.get('member_id') is None else
                      'no_record' if available else 'source_unavailable',
            'last_active_day': None, 'active_streak_days': None, 'lifetime_messages': None})
    return {'members_checked': len(identifiers) if available else 0,
            'members_not_checked': 0 if available else len(identifiers),
            'source_reads_complete': available,
            'basis': 'Achievements user_stats posting records from all channels the bot sees; '
                'last_active_day is a UTC date, not an exact last-message time. Streak and lifetime '
                'messages are stored counters, not counts within the requested war period. '
                'Records are deleted when members leave.'}
