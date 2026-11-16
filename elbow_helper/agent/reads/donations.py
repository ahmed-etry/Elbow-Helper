"""Stored donations and received over a period, per account or summed per member, with polling coverage.

The figures are clan health's own calculation over the family record's donation samples; nothing
here asks Clash. Ownership is the current bot links.
"""
from datetime import datetime, timezone

from elbow_helper.agent.engine.validation import invalid_arguments
from elbow_helper.agent.reads.snapshots import DATE, IDS, TAGS, family_tool
from .people import PeopleAdapter, PeopleSelection, record
from .projections import COHORT_REF
from .war_tools import CLANS_INPUT, WINDOW_RULES

SECOND = 1_000_000
# Accounts per record read, as the record's reads accept them.
BATCH = 200
STAY_LIMIT = 100_000
SELECTORS = ['clan_codes', 'member_ids', 'account_tags', 'cohort_ref']
SELECTOR_ERROR = 'Choose clans, members, accounts or a cohort.'
FIGURES = ('donations', 'donations_received')
BASIS = ('Stored donation counter samples: each figure adds the rises between samples, and a fall '
         '(season reset or clan move) counts the new value in full.')
COVERAGE = {
    'C': 'Polled at both ends of the period and around every counter reset: complete; zero means none.',
    'P': 'Polling missed the start, the end or a counter reset: a lower bound.',
    'N': 'Not polled and no samples in the period: unknown, not zero.'}
CLAN_POPULATION = ('Accounts seen in the selected clans; donations and donations_received cover all clans, '
                   'while clan_donations and clan_donations_received cover only the selected clans.')
POPULATION = 'The selected accounts; figures count donations in any clan.'
AVERAGE_BASIS = ('Each period counts each account seen in the selected clans once, including zero; '
                 'unknown accounts are counted separately and excluded from averages. Whole-period figures '
                 'cover all clans; selected-clan figures use only the selected clans\' roster samples. '
                 'Totals and per-account averages use all account rows before member grouping or paging.')


def micros(moment):
    return int(moment.timestamp() * SECOND)


def months(after, before):
    """The window cut at UTC calendar month starts, as (label, start, end)."""
    windows, start = [], after
    while start < before:
        following = datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=timezone.utc)
        end = min(following, before)
        windows.append((f'{start:%Y-%m}', start, end))
        start = end
    return windows


def window(context, arguments):
    try:
        after = datetime.fromisoformat(arguments['after'])
        before = (datetime.fromisoformat(arguments['before']) if arguments.get('before')
                  else context.source_message.created_at)
        if after.utcoffset() is None or before.utcoffset() is None or before <= after:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return None
    return after.astimezone(timezone.utc), before.astimezone(timezone.utc)


async def clan_accounts(context, codes, windows):
    """Per window, the accounts with a stay in the selected clans during it; None when unreadable."""
    snapshot = await record(context, 'stay_reads', 'movements', after=micros(windows[0][1]),
                            before=micros(windows[-1][2]), limit=STAY_LIMIT)
    if snapshot is None or snapshot['truncated']:
        return None
    present = []
    for _, start, end in windows:
        start, end = micros(start), micros(end)
        present.append({account['player_tag'] for account in snapshot['rows'] for stay in account['stays']
                        if stay['clan_code'] in codes and stay['first_seen_at'] < end
                        and (stay['to_at'] is None or stay['last_seen_at'] >= start)})
    return present


async def selected_accounts(context, arguments, windows):
    """(account tags, per-window accounts or None, unlinked member IDs), or an error."""
    if arguments.get('clan_codes'):
        present = await clan_accounts(context, set(arguments['clan_codes']), windows)
        if present is None:
            return {'error': 'Stored clan memberships could not be read.', 'cause': 'source_unavailable'}
        return sorted(set().union(*present)), present, []
    if arguments.get('cohort_ref'):
        from .projections import cohort_selection
        selection = await cohort_selection(context, arguments['cohort_ref'], 'account_tags')
        if 'error' in selection:
            return selection
        return selection['account_tags'], None, []
    if arguments.get('member_ids'):
        links = await PeopleAdapter(context).links(PeopleSelection('members', tuple(arguments['member_ids'])))
        if links.status != 'available':
            return {'error': 'Account links are not available.', 'cause': links.status}
        tags = sorted({row['player_tag'] for rows in links.by_member.values() for row in rows if row.get('player_tag')})
        return tags, None, [member for member in arguments['member_ids'] if not links.by_member.get(member)]
    return arguments['account_tags'], None, []


def summed(group, prefix=''):
    """A member's coverage and figures from its accounts'."""
    states = {account[prefix + 'coverage'] for account in group}
    state = states.pop() if len(states) == 1 else 'P'
    return {prefix + 'coverage': state, **{prefix + name: None if state == 'N' else sum(
        account[prefix + name] for account in group if account[prefix + name] is not None) for name in FIGURES}}


def averages(group, prefix=''):
    totals = summed(group, prefix) if group else {prefix + 'coverage': 'N',
                                                **dict.fromkeys((prefix + name for name in FIGURES))}
    known = sum(account[prefix + 'coverage'] != 'N' for account in group)
    return {'coverage': totals[prefix + 'coverage'], 'known_account_count': known,
            'unknown_account_count': len(group) - known,
            **{name: totals[prefix + name] for name in FIGURES},
            **{'average_' + name: totals[prefix + name] / known if known else None for name in FIGURES}}


async def donation_history(context, arguments):
    if getattr(getattr(context, 'bot', None), 'activity_reads', None) is None:
        return {'error': 'Stored donations are not available.', 'cause': 'missing_setup'}
    period = window(context, arguments)
    if period is None:
        return invalid_arguments('Choose a complete date window with its end after its start.', 'after', 'before')
    after, before = period
    by_month = arguments.get('by_month', False)
    windows = months(after, before) if by_month else [(None, after, before)]
    selection = await selected_accounts(context, arguments, windows)
    if isinstance(selection, dict):
        return selection
    tags, present, unlinked = selection
    figures, names = [], {}
    for start in range(0, len(tags), BATCH):
        chunk = tags[start:start + BATCH]
        page = await record(context, 'activity_reads', 'donation_windows', chunk,
                            windows=[(micros(begin), micros(end)) for _, begin, end in windows],
                            **({'clan_codes': arguments['clan_codes']}
                               if present is not None else {}))
        directory = await record(context, 'stay_reads', 'accounts', chunk)
        if page is None or directory is None:
            return {'error': 'Stored donations could not be read.', 'cause': 'source_unavailable'}
        figures.extend(page['rows'])
        names.update((row['player_tag'], row['player_name']) for row in directory['rows'])
    links = await PeopleAdapter(context).links(PeopleSelection('accounts', tuple(tags)))
    if links.status == 'source_unavailable':
        return {'error': 'Account links are not available.', 'cause': 'source_unavailable'}
    owners = {tag: (row or {}).get('discord_user_id') for tag, row in links.by_tag.items()}

    def member_name(owner):
        member = context.guild.get_member(owner) if owner else None
        return member.display_name if member else None

    positions = {(micros(begin), micros(end)): index for index, (_, begin, end) in enumerate(windows)}
    accounts = []
    for row in figures:
        index = positions[(row['after'], row['before'])]
        if present is not None and row['player_tag'] not in present[index]:
            continue
        owner = owners.get(row['player_tag'])
        accounts.append((index, {**({'month': windows[index][0]} if by_month else {}),
            'player_tag': row['player_tag'], 'player_name': names.get(row['player_tag']),
            'member_id': owner, 'member_name': member_name(owner),
            **{key: row[key] for key in (*FIGURES, 'coverage')},
            **{key: row[key] for key in ('clan_donations', 'clan_donations_received', 'clan_coverage')
               if present is not None}}))
    clan_totals = []
    if present is not None:
        for index, (label, begin, end) in enumerate(windows):
            group = [account for position, account in accounts if position == index]
            clan_totals.append({**({'month': label} if by_month else {}),
                'after': begin.isoformat(), 'before': end.isoformat(), 'account_count': len(group),
                'whole_period': averages(group), 'selected_clans': averages(group, 'clan_')})
    unit = arguments.get('unit', 'account')
    if unit == 'member':
        groups = {}
        for index, account in accounts:
            key = (index, ('member', account['member_id']) if account['member_id'] else ('account', account['player_tag']))
            groups.setdefault(key, []).append(account)
        accounts = []
        for (index, _), group in groups.items():
            accounts.append((index, {**({'month': windows[index][0]} if by_month else {}),
                'member_id': group[0]['member_id'], 'member_name': group[0]['member_name'],
                'account_tags': sorted(account['player_tag'] for account in group), **summed(group),
                **(summed(group, 'clan_') if present is not None else {})}))
    order = 'clan_donations' if present is not None else 'donations'
    accounts.sort(key=lambda item: (item[0], item[1][order] is None, -(item[1][order] or 0),
                                    item[1]['member_id'] or 0, item[1].get('player_tag') or ''))
    return {'rows': [row for _, row in accounts], 'unit': unit,
            'after': after.isoformat(), 'before': before.isoformat(),
            **({'months': [{'month': label, 'after': begin.isoformat(), 'before': end.isoformat()}
                           for label, begin, end in windows]} if by_month else {}),
            'account_count': len(tags), 'population': CLAN_POPULATION if present is not None else POPULATION,
            'basis': BASIS, 'coverage': COVERAGE,
            **({'clan_totals': clan_totals, 'average_basis': AVERAGE_BASIS} if present is not None else {}),
            **({'clan_basis': 'Clan figures use only clan-roster samples and count the first value after a clan '
                'change in full; clan_coverage describes polling in the selected clans.'} if present is not None else {}),
            'ownership_basis': 'current bot links; historical ownership unknown',
            **({'unlinked_member_ids': unlinked} if unlinked else {})}


TOOLS = (family_tool('read_donation_history',
    'Read stored donations and received per account or member over a period or by month, with coverage.',
    {'clan_codes': CLANS_INPUT, 'member_ids': IDS, 'account_tags': TAGS, 'cohort_ref': COHORT_REF,
     'after': DATE, 'before': DATE, 'by_month': {'type': 'boolean'},
     'unit': {'type': 'string', 'enum': ['account', 'member']}},
    donation_history, required=('after',),
    rules=(*WINDOW_RULES, {'needs': SELECTORS, 'error': SELECTOR_ERROR},
           {'only_one': SELECTORS, 'error': SELECTOR_ERROR}),
    whole=True),)
