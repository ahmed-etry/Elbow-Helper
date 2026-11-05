"""Typed account projections and authorized cohort references shared by family reads."""
import json

from .core import FamilyReport, UNKEPT_REPORT
from ..reports.tools import authorize_report
from ..reads.wars import performance_projection
from ..reads.people import PeopleAdapter, PeopleSelection

__all__ = ['performance_projection', 'cohort_selection', 'ACCOUNT_FILTERS', 'COHORT_REF', 'PERFORMANCE',
           'account_matches', 'unknown_account_filter', 'unknown_account_data']


ACCOUNT_FILTERS = {
    'town_hall_min':{'type':'integer','minimum':1,'maximum':100},
    'town_hall_max':{'type':'integer','minimum':1,'maximum':100},
    'ranked_tier_ids':{'type':'array','minItems':1,'maxItems':100,
                       'items':{'type':'integer','minimum':1}},
    'ranked_tier_prefix':{'type':'string','minLength':1,'maxLength':100,
        'description':'Literal prefix of the API-owned leagueTier.name; never a player name or message.'},
}
COHORT_REF = {'type':'string','minLength':1,'maxLength':32}
PERFORMANCE = {'type':'object','properties':{
    'war_type':{'type':'string','enum':['REG','CWL','both']},
    'latest_completed':{'type':'integer','minimum':1,'maximum':100},
    'season':{'type':'string','maxLength':20},
    'after':{'type':'string','format':'date-time'},'before':{'type':'string','format':'date-time'},
    'clan_codes':{'type':'array','minItems':1,'maxItems':9,'items':{'type':'string',
        'enum':['BEH','BE4','BES','BE1','BEM','BEC','BEE','BEP','BEW']}}},
    'required':['war_type'],'additionalProperties':False}


def account_matches(row, arguments):
    th = row.get('townHallLevel')
    if any(key in arguments and (th is None or (th < arguments[key] if key.endswith('min')
            else th > arguments[key])) for key in ('town_hall_min','town_hall_max')):
        return False
    if arguments.get('ranked_tier_prefix'):
        league = row.get('ranked_league')
        if not isinstance(league,dict) or not str(league.get('name') or '').casefold().startswith(
                arguments['ranked_tier_prefix'].casefold()):
            return False
    return not arguments.get('ranked_tier_ids') or (
        isinstance(row.get('ranked_league'),dict)
        and row['ranked_league'].get('id') in arguments['ranked_tier_ids'])


def unknown_account_filter(row, arguments):
    if row.get('profile_status')=='unlinked':
        return False
    league = row.get('ranked_league')
    return (any(key in arguments for key in ('town_hall_min','town_hall_max')) and row.get('townHallLevel') is None
        or bool(arguments.get('ranked_tier_ids')) and (not isinstance(league,dict) or league.get('id') is None)
        or bool(arguments.get('ranked_tier_prefix')) and (not isinstance(league,dict) or not league.get('name')))


def unknown_account_data(row, arguments):
    return row.get('profile_status')!='unlinked' and (
        row.get('profile_status')!='ok' or unknown_account_filter(row,arguments))


async def cohort_selection(context, identifier, selector):
    report = context.state.reports.get(identifier)
    if report is None and identifier in context.state.unkept_reports:
        return dict(UNKEPT_REPORT)
    if (not isinstance(report,FamilyReport) or report.family not in (
        'read_member_cohort','read_member_accounts','read_clan_rosters','read_recruitment_pipeline','read_cwl_results')
            or not await authorize_report(context,identifier)):
        return {'error':'That cohort is no longer available here. Read it again.','cause':'report_unavailable'}
    rows = json.loads(report.payload)['rows']
    if json.loads(report.payload)['summary'].get('requester_id', context.member.id) != context.member.id:
        return {'error':'Read a new cohort for this request.','cause':'report_unavailable'}
    members = sorted({row.get('member_id') or row.get('applicant_member_id') for row in rows
                      if row.get('member_id') or row.get('applicant_member_id')})
    tags = {row['player_tag'] for row in rows if row.get('player_tag')}
    tags.update(account['player_tag'] for row in rows for account in row.get('accounts', [])
                if account.get('player_tag'))
    tags.update(tag for row in rows for tag in row.get('account_tags', []))
    selected_accounts = any('accounts' in row or 'player_tag' in row for row in rows)
    if (selector=='account_tags' and not selected_accounts and members
            and context.account_links):
        selected = await PeopleAdapter(context).links(PeopleSelection('members', tuple(members)))
        if selected.status != 'available':
            return {'error': 'Account links are not available.', 'cause': selected.status}
        links = selected.by_member
        tags = {row['player_tag'] for values in links.values() for row in values}
    values = members if selector=='member_ids' else sorted(tags)
    if selector == 'member_ids' and tags and report.family in ('read_cwl_results', 'read_clan_rosters', 'read_member_accounts'):
        selector, values = 'account_tags', sorted(tags)
    if selector=='member_ids' and not values and tags:
        selector,values = 'account_tags',sorted(tags)
    if not values:
        if json.loads(report.payload)['summary'].get('count_complete') is False:
            return {'error': 'The source cohort could not be resolved completely.',
                    'cause': 'source_unavailable', 'cohort_ref': identifier}
        return {'error':'That cohort has no resolved '+('members' if selector=='member_ids' else 'accounts')+'.',
                'cause':'empty_cohort'}
    return {selector:values}
