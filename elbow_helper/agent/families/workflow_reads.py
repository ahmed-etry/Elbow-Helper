"""Scoped recruitment, hibernation and promotion workflow reads."""
from dataclasses import asdict
import asyncio
from datetime import datetime, timezone

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.features.examination.queries import CASE_TYPES
from .core import family_tool, IDS
from .identity_reads import member_fields
from .projections import PERFORMANCE, performance_projection
from .workflow_events import workflow_events, event_order, group_pipeline
from ..access import accessible_message_channel, has_lookup_access, ACCESS_RECRUITMENT, ACCESS_PROMOTION
from ..capabilities.hibernation.reads import read_active_hibernation
from ..capabilities.examination.promotion_route import read_promotion_review
from ..capabilities.member_lifecycle.reads import read_member_lifecycle
from ..datasets.query import query_context
from ..datasets.clash import read_clash
from ..datasets.battle_logs import battle_date
from ..research.history import read_discord_channel_history


async def complete_snapshot(context, reader, arguments, field, output_field=None):
    before = set(context.state.reports)
    result = await reader(context, arguments)
    if 'error' in result:
        return result
    report = context.state.reports.get(result.get('report_id'))
    rows = [asdict(row) for row in getattr(report.snapshot, field)] if report else result.get(output_field or field, [])
    if report is None and result.get('next_offset') is not None:
        return {'error': 'The complete workflow snapshot could not be retained.', 'cause': 'capacity'}
    if report and report.report_id not in before:
        context.state.reports.pop(report.report_id)
        context.state.report_sources.pop(report.report_id, None)
        context.state.report_access_requirements.pop(report.report_id, None)
    summary = {key: value for key, value in result.items() if key not in {
        'report_id','kind','rows','members','records','trials','cases','tickets','next_offset','offset'}}
    return {**summary, 'rows': rows}


async def recruitment(context, arguments):
    if arguments.get('stage') == 'requester_tickets':
        return await requester_tickets(context, arguments)
    if not has_lookup_access(context, {ACCESS_RECRUITMENT}):
        return {'error': 'This needs recruitment access.', 'cause': 'scope_refused',
                'required_access': [ACCESS_RECRUITMENT]}
    context.state.required_access.add(ACCESS_RECRUITMENT)
    stage = arguments.get('stage', 'pipeline')
    if stage in ('acceptance_history', 'ticket_log'):
        from .recruitment_history import recruitment_history
        return await recruitment_history(context, arguments)
    ticket = arguments.get('ticket_channel_id')
    if stage == 'pipeline':
        gathered, missing = [], []
        for part in ('tickets', 'active_trials', 'trial_followups', 'ticket_reminders', 'lifecycle'):
            selected = {key: value for key, value in arguments.items()
                        if key not in ('include_accounts', 'performance', 'after', 'before')}
            source = await recruitment(context, {**selected, 'stage': part})
            if 'error' in source:
                missing.append({'stage': part, 'cause': source.get('cause', 'source_unavailable')})
            else:
                gathered.extend(dict(event, evidence_kind=part) for row in source['rows']
                                for event in row.get('events', workflow_events(row)))
        result = {'rows': gathered, 'missing_sources': missing,
            'coverage_note': 'Accessible current workflow evidence joined by ticket and member IDs; historical decisions require their own evidence.'}
    elif stage == 'active_trials':
        result = await active_trial_rows(context, ticket)
    elif stage == 'lifecycle':
        result = await complete_snapshot(context, read_member_lifecycle, {}, 'rows', 'members')
    elif stage in ('trial_followups','ticket_reminders'):
        if stage == 'trial_followups':
            sql = '''SELECT CAST(e.key AS INTEGER) AS ticket_channel_id,
                json_extract(e.value,'$.applicant_id') AS applicant_member_id,
                json_extract(e.value,'$.created_at') AS created_at,
                json_extract(e.value,'$.resolved_at') AS trial_ended_at,
                json_extract(e.value,'$.message_id') AS message_id
                FROM state.recruitment_trial_reminders s, json_each(s.doc) e
                ORDER BY ticket_channel_id'''
        else:
            sql = '''SELECT CAST(e.key AS INTEGER) AS ticket_channel_id,
                json_extract(e.value,'$.last_reminder_at') AS last_reminder_at,
                json_extract(e.value,'$.last_applicant_message_id') AS last_applicant_message_id
                FROM state.recruitment_ticket_activity s, json_each(s.doc) e
                ORDER BY ticket_channel_id'''
        result = await query_context(context, sql, max_rows=None)
        if 'error' not in result:
            for row in result['rows']:
                row['workflow_status'] = ('trial_ended_followup_recorded' if row.get('trial_ended_at') is not None
                    else 'trial_end_pending') if stage == 'trial_followups' else 'applicant_response_reminder'
                row.update(acceptance_decision=None, finalized=None)
                if stage=='trial_followups':
                    row['followup_delivery_status'] = 'unverified; check ticket evidence'
    elif stage == 'ticket_evidence':
        if not ticket or not arguments.get('after'):
            return {'error': 'Which recruitment ticket and date window should I read?', 'cause': 'needs_input'}
        channel = await accessible_message_channel(context, ticket)
        if channel is None or getattr(channel, 'category_id', None) != RECRUITMENT_TICKET_CATEGORY:
            return {'error': 'That recruitment ticket is unavailable here.', 'cause': 'scope_refused'}
        result = await read_discord_channel_history(context, {'channel_id':ticket,
            'after':arguments['after'], **({'before':arguments['before']} if arguments.get('before') else {}),
            **({'cursor':arguments['source_cursor']} if arguments.get('source_cursor') else {}), 'limit':100},
            complete_content=True)
        if 'error' not in result:
            messages = result.pop('messages', [])
            result = {**result, 'rows':[dict(row,ticket_channel_id=ticket) for row in messages],
                      'source_next_cursor':result.get('coverage',{}).get('next_cursor'),
                      'coverage_note':'Ticket statements need interpretation; no decision is inferred from a reminder.'}
    else:
        rows = []
        for channel in sorted(getattr(context.guild, 'channels', ()), key=lambda channel:channel.id):
            if (getattr(channel,'category_id',None) == RECRUITMENT_TICKET_CATEGORY
                    and (not ticket or channel.id == ticket)
                    and await accessible_message_channel(context,channel.id) is not None):
                rows.append({'ticket_channel_id':channel.id,'name':channel.name,
                             'acceptance_decision':None,'finalized':None})
                context.state.source_channels.add(channel.id)
        result = {'rows':rows, 'coverage_note':'Accessible recruitment tickets; opening a ticket is not acceptance.'}
    if 'error' in result:
        return result
    if stage not in ('pipeline', 'ticket_evidence') and (arguments.get('after') or arguments.get('before')):
        result['rows'] = [dict(row, events=workflow_events(row)) for row in result['rows']]
    if stage != 'ticket_evidence' and (arguments.get('after') or arguments.get('before')):
        try:
            after = datetime.fromisoformat(arguments['after']) if arguments.get('after') else None
            before = datetime.fromisoformat(arguments['before']) if arguments.get('before') else context.source_message.created_at
            if after is None or after.utcoffset() is None or before.utcoffset() is None or after >= before:
                raise ValueError('Invalid workflow period')
        except (ValueError, TypeError):
            return {'error': 'Choose a timezone-aware start before the workflow window ends.', 'cause': 'invalid_arguments'}
        selected_rows = []
        for row in result['rows']:
            events = row.get('events', [row])
            in_period = sorted([event for event in events if event['event_ts'] is not None
                and after.timestamp() <= event['event_ts'] < before.timestamp()], key=event_order)
            undated = [event for event in events if event['event_ts'] is None]
            if in_period or undated:
                if stage == 'pipeline':
                    selected_rows.extend(in_period + undated)
                else:
                    selected_rows.append(dict(row, period_status='matched' if in_period else 'unknown_date',
                        events=sorted(in_period + undated, key=event_order),
                        period_evidence=in_period, undated_evidence=undated))
        result.update(rows=selected_rows, resolved_period={'after': after.isoformat(), 'before': before.isoformat()},
            period_selection_complete=not any(row.get('undated_evidence') or (
                stage == 'pipeline' and row['event_ts'] is None) for row in selected_rows),
            period_meaning='Recorded workflow events, not a reconstruction of all historical applicants or decisions.')
    if stage == 'pipeline':
        result['rows'] = group_pipeline(result['rows'], dated='resolved_period' in result)
    result['rows'] = [row for row in result['rows'] if (
        not ticket or row.get('ticket_channel_id') == ticket) and (
        not arguments.get('member_ids') or row.get('applicant_member_id',row.get('member_id')) is None
        or row.get('applicant_member_id', row.get('member_id')) in arguments['member_ids'])]
    result['unresolved_member_selection_rows'] = sum(
        row.get('applicant_member_id',row.get('member_id')) is None for row in result['rows']) if arguments.get('member_ids') else 0
    result['member_selection_complete'] = not result['unresolved_member_selection_rows']
    if arguments.get('include_accounts'):
        from .identity_reads import member_accounts
        ids = sorted({row.get('member_id') or row.get('applicant_member_id') for row in result['rows']
                      if row.get('member_id') or row.get('applicant_member_id')})
        accounts = await member_accounts(context,{'member_ids':ids}) if ids else {'rows':[]}
        result['members'] = accounts.get('members', [])
        for row in result['rows']:
            identifier = row.get('member_id') or row.get('applicant_member_id')
            row['accounts'] = [account for account in accounts.get('rows',[]) if identifier and account.get('member_id')==identifier]
        result['account_coverage'] = accounts.get('error') or accounts.get('coverage') or 'No resolved applicant IDs.'
    performance = arguments.get('performance')
    if performance is None and arguments.get('include_accounts') and stage in ('active_trials', 'pipeline', 'lifecycle'):
        performance = {'war_type': 'both', 'latest_completed': 30}
    if performance:
        result['performance_coverage'] = await performance_projection(context, result['rows'], performance)
    return {**result, 'stage':stage,
            'history_complete':False,
            'workflow_meaning':'A trial-end follow-up records that the trial ended; it is not a later accept, decline or finalize decision.'}


async def requester_tickets(context, arguments):
    """Find only the requester's own readable tickets by authoritative identity.

    A channel name, a read grant or a participant mention in member prose is
    never ownership evidence. The original bot opener is a structured source.
    """
    if (arguments.get('member_ids') not in (None, [context.member.id])
            or any(arguments.get(key) for key in ('performance', 'after', 'before', 'source_cursor'))):
        return {'error': 'That recruitment ticket is unavailable here.', 'cause': 'scope_refused'}
    candidates, private_available = [], False
    for channel in sorted(context.guild.text_channels, key=lambda item: item.id):
        if (channel.category_id != RECRUITMENT_TICKET_CATEGORY
                or arguments.get('ticket_channel_id') not in (None, channel.id)):
            continue
        permissions = channel.permissions_for(context.member)
        bot_permissions = channel.permissions_for(context.guild.me)
        if not (permissions.view_channel and permissions.read_message_history
                and bot_permissions.view_channel and bot_permissions.read_message_history):
            continue
        if await accessible_message_channel(context, channel.id) is not None:
            candidates.append(channel)
        elif str(channel.id) in context.state.scope_audit.private_candidates:
            private_available = True
    owners, missing = {}, []
    if context.recruitment_queries is not None and candidates:
        try:
            snapshot = await asyncio.to_thread(context.recruitment_queries.active_trial_snapshot,
                ticket_channel_ids=tuple(channel.id for channel in candidates))
            owners = {row.ticket_channel_id: row.applicant_member_id for row in snapshot.trials}
        except (ValueError, OSError):
            pass  # The opener can still establish ownership independently.
    rows = []
    for channel in candidates:
        owner = owners.get(channel.id)
        basis, opener = 'registered_applicant_id', None
        if owner is None:
            try:
                first = [msg async for msg in channel.history(limit=1, oldest_first=True)]
            except (discord.HTTPException, OSError):
                first = []
            if first and getattr(first[0].author, 'bot', False):
                mentions = getattr(first[0], 'mentions', ())
                if len(mentions) == 1:
                    owner, opener = mentions[0].id, first[0]
                    basis = 'bot_opener_member_mention'
        if owner is None:
            missing.append({'ticket_channel_id': channel.id, 'cause': 'ownership_unknown'})
            continue
        if owner != context.member.id:
            continue
        context.state.source_channels.add(channel.id)
        rows.append({'ticket_channel_id': channel.id, 'name': channel.name,
                     'applicant_member_id': owner, 'ownership_basis': basis,
                     'source': opener.jump_url if opener else
                         f'https://discord.com/channels/{context.guild.id}/{channel.id}',
                     'acceptance_decision': None, 'finalized': None})
    members = []
    if arguments.get('include_accounts') and rows:
        from .identity_reads import member_accounts
        accounts = await member_accounts(context, {'member_ids': [context.member.id]})
        members = accounts.get('members', [])
        for row in rows:
            row['accounts'] = accounts.get('rows', [])
    # Unmatched tickets remain unknown; no other applicant's data is returned.
    return {'rows': rows, 'members': members, 'stage': 'requester_tickets', 'requester_id': context.member.id,
            'ownership_complete': not missing, 'unresolved_ticket_count': len(missing),
            'available_privately': private_available,
            'coverage': 'Current readable recruitment tickets owned by the requester; deleted and archived tickets are not reconstructed.'}


async def active_trial_rows(context, ticket=None):
    """Department-authorized status records survive deleted tickets and departures.

    This reads the recruitment store, not private ticket messages. Ticket content
    continues to require channel access in the ticket_evidence stage.
    """
    if not has_lookup_access(context, {ACCESS_RECRUITMENT}):
        return {'error': 'This needs recruitment access.', 'cause': 'scope_refused',
                'required_access': [ACCESS_RECRUITMENT]}
    context.state.required_access.add(ACCESS_RECRUITMENT)
    if context.recruitment_queries is None:
        return {'error': 'Recruitment trial status is not available.', 'cause': 'missing_setup'}
    registrations = context.recruitment_queries.active_trial_registrations()
    snapshot = context.recruitment_queries.active_trial_snapshot(ticket_channel_ids=tuple(
        row.ticket_channel_id for row in registrations if ticket is None or row.ticket_channel_id == ticket))
    return {**{key: value for key, value in asdict(snapshot).items() if key != 'trials'},
            'rows': [asdict(row) for row in snapshot.trials],
            'coverage': 'All registered trial status records; ticket content is separately authorized.'}


async def hibernation(context, arguments):
    result = await complete_snapshot(context, read_active_hibernation, {'limit':500}, 'records')
    if 'error' in result:
        return result
    rows = [dict(row, **member_fields(context.guild.get_member(row['member_id']),row['member_id']))
            for row in result['rows'] if not arguments.get('member_ids') or row['member_id'] in arguments['member_ids']]
    if arguments.get('include_activity'):
        identifiers = [row['member_id'] for row in rows]
        links = await asyncio.to_thread(context.account_links.get_links_for_members, identifiers) if context.account_links else {}
        starts = {link['player_tag']:row['started_ts'] for row in rows if row['started_ts'] is not None
                  for link in links.get(row['member_id'], [])}
        logs = {}
        kinds = arguments.get('activity_kinds', ['ranked', 'regular_war', 'cwl'])
        war_rows = None
        if starts and set(kinds).intersection(('regular_war', 'cwl')):
            from .war_reads import selected_wars
            period = {'after': datetime.fromtimestamp(min(starts.values()), timezone.utc).isoformat(),
                      'before': context.source_message.created_at.isoformat()}
            wars = await selected_wars(context, period, 'both')
            if 'error' not in wars:
                war_rows = wars['rows']
        tags = sorted(starts)
        for start in range(0,len(tags) if 'ranked' in kinds else 0,20):
            source = await read_clash(context, {'kind':'battle_log','tags':tags[start:start+20],
                                                'detail':'full','limit':50})
            if 'error' not in source:
                logs.update({row['tag']:row for row in source['items']})
        for row in rows:
            accounts = []
            for link in links.get(row['member_id'], []):
                tag = link['player_tag']
                item = logs.get(tag, {'status':'unavailable'})
                attacks = [battle_date(battle) for battle in item.get('data',{}).get('items',[])
                    if str(battle.get('battleType','')).upper() in ('RANKED','LEGEND')
                    and battle.get('attack') is True and battle_date(battle) is not None
                    and row['started_ts'] is not None and battle_date(battle).timestamp() >= row['started_ts']]
                accounts.append({'player_tag':tag,'profile_status':item['status'],
                    'observed_ranked_attacks_since_start':len(attacks) if item['status']=='ok' and row['started_ts'] is not None else None,
                    'latest_observed_ranked_attack_at':max(attacks).isoformat() if attacks else None})
                account = accounts[-1]
                for kind, war_type in [('regular_war', 'REG'), ('cwl', 'CWL')]:
                    if kind not in kinds:
                        continue
                    evidence = [war for war in war_rows or [] if war.get('war_type') == war_type
                        and war['player_tag'] == tag and row['started_ts'] is not None
                        and war['end_ts'] > row['started_ts']]
                    within = [war for war in evidence if war.get('start_ts') is not None
                              and war['start_ts'] >= row['started_ts']]
                    overlapping = [war for war in evidence if war not in within]
                    post_start_attacks = (
                        sum(war['attacks_used'] for war in within)
                        if war_rows is not None and row['started_ts'] is not None
                        and all(war['final_capture'] for war in within) else None)
                    account['observed_' + kind + '_attacks_in_post_start_wars'] = post_start_attacks
                    account['observed_' + kind + '_attacks_since_start'] = (
                        post_start_attacks if not overlapping else None)
                    account[kind + '_overlapping_war_participation'] = [
                        {'war_id': war['war_id'], 'war_start_ts': war.get('start_ts'),
                         'war_end_ts': war['end_ts'], 'observed_attacks': war['attacks_used'],
                         'attacks_since_start': None, 'final_capture': war['final_capture']}
                        for war in overlapping]
                    latest = max((war['end_ts'] for war in evidence if war['attacks_used']), default=None)
                    end_field = ('latest_observed_regular_war_end_at' if war_type == 'REG'
                                 else 'latest_observed_cwl_war_end_at')
                    account[end_field] = (
                        datetime.fromtimestamp(latest, timezone.utc).isoformat() if latest is not None else None)
            row.update(accounts=accounts, activity_coverage='Ranked activity uses latest rolling logs; absence does not establish inactivity.',
                       war_activity_coverage='Stored wars have no individual attack timestamps. Counts use wars starting at or after hibernation; overlapping or undated-start wars retain unknown attack timing. Latest war-end dates are not attack dates.',
                       ownership_basis='current bot links; historical ownership unknown')
    sort_field = 'started_at' if arguments.get('sort') == 'longest_hibernating' else 'joined_at'
    rows.sort(key=lambda row:(row.get(sort_field) is None,row.get(sort_field) or '',row['member_id']))
    return {**result,'rows':rows,'coverage_note':'Active workflow records, not role holders. Missing start times and activity remain unknown.'}


async def promotion(context, arguments):
    if not has_lookup_access(context, {ACCESS_PROMOTION}):
        return {'error': 'This needs promotion access.', 'cause': 'scope_refused',
                'required_access': [ACCESS_PROMOTION]}
    context.state.required_access.add(ACCESS_PROMOTION)
    if context.examination_queries is None:
        return {'error': 'Promotion cases are not available.', 'cause': 'missing_setup'}
    snapshot = context.examination_queries.case_snapshot(ticket_channel_ids=tuple(
        row.ticket_channel_id for row in context.examination_queries.case_registrations()
        if not arguments.get('ticket_channel_id') or row.ticket_channel_id == arguments['ticket_channel_id']))
    result = {**{key: value for key, value in asdict(snapshot).items() if key != 'cases'},
              'rows': [asdict(row) for row in snapshot.cases]}
    if 'error' in result:
        return result
    result['rows'] = sorted([row for row in result['rows'] if (
        not arguments.get('member_ids') or row['applicant_member_id'] in arguments['member_ids']) and (
        not arguments.get('case_type') or row['case_type']==arguments['case_type'])],
        key=lambda row:(row['workflow_status'],row['ticket_channel_id']))
    if arguments.get('include_review'):
        for row in result['rows']:
            review = (await read_promotion_review(context, {'ticket_channel_id': row['ticket_channel_id'], 'view': 'details'})
                      if callable(getattr(context.bot, 'get_cog', None)) else {'cause': 'missing_setup'})
            row['review'] = review if 'error' not in review and 'text' in review else None
            row['review_status'] = 'available' if row['review'] else 'unavailable'
    if arguments.get('performance'):
        result['performance_coverage'] = await performance_projection(context, result['rows'], arguments['performance'])
    return {**result,'workflow_meaning':'Intake, examiner routing and recorded response are distinct; routing does not establish a promotion decision.'}


TICKET = {'type':'integer','minimum':1}
TOOLS = (
 family_tool('read_recruitment_pipeline','Read recruitment tickets, active trials, follow-ups and lifecycle evidence.',
    {'stage':{'type':'string','enum':['pipeline','tickets','requester_tickets','active_trials','trial_followups','ticket_reminders','lifecycle','ticket_evidence','acceptance_history','ticket_log']},
     'ticket_channel_id':TICKET,'member_ids':IDS,'after':{'type':'string','format':'date-time'},
     'include_accounts':{'type':'boolean'},'performance':PERFORMANCE,
     'before':{'type':'string','format':'date-time'},'source_cursor':{'type':'string','maxLength':200}}, recruitment,
     usage_note='requester_tickets: requester-owned readable tickets, no staff access. Other stages require recruitment access. Destination scope applies.'),
 family_tool('read_hibernation','Read active hibernation, Discord join dates and observed activity since its start.',
    {'member_ids':IDS,'include_activity':{'type':'boolean'},
     'activity_kinds': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                       'items': {'type': 'string', 'enum': ['ranked', 'regular_war', 'cwl']}},
     'sort': {'type': 'string', 'enum': ['oldest_server_join', 'longest_hibernating']}}, hibernation),
 family_tool('read_promotion_cases','Read scoped promotion intake, examiner routing and recorded responses.',
    {'ticket_channel_id':TICKET,'member_ids':IDS,'case_type':{'type':'string','enum':sorted(CASE_TYPES)},
     'include_review': {'type': 'boolean'}, 'performance': PERFORMANCE}, promotion),
)
