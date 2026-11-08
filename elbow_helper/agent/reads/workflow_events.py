"""Recorded workflow timestamps are separate typed events."""
from datetime import datetime, timezone


def workflow_events(record):
    events = []
    for field, kind in (
        ('created_at', 'record_created'), ('start_at', 'trial_started'),
        ('last_reminder_at', 'reminder_recorded'), ('trial_ended_at', 'trial_ended'),
    ):
        value = record.get(field)
        if value is None:
            continue
        try:
            date = (datetime.fromtimestamp(value, timezone.utc) if type(value) in (int, float)
                    else datetime.fromisoformat(value))
            timestamp = date.timestamp() if date.utcoffset() is not None else None
        except (ValueError, TypeError, OverflowError, OSError):
            timestamp = None
        events.append({**record, 'event_kind': kind,
                       'event_at': date.isoformat() if timestamp is not None else None,
                       'event_ts': timestamp})
    return events or [{**record, 'event_kind': 'undated', 'event_at': None, 'event_ts': None}]


def event_order(event):
    return (event['event_ts'] is None, event['event_ts'] or 0,
            event.get('message_id') or 0, event.get('evidence_kind', ''), event['event_kind'])


def group_pipeline(events, *, dated=False):
    groups, tickets_by_member = {}, {}
    for event in events:
        member = event.get('applicant_member_id') or event.get('member_id')
        if member and event.get('ticket_channel_id'):
            tickets_by_member.setdefault(member, set()).add(event['ticket_channel_id'])
    for event in sorted(events, key=event_order):
        member = event.get('applicant_member_id') or event.get('member_id')
        ticket = event.get('ticket_channel_id')
        if not ticket and len(tickets_by_member.get(member, ())) == 1:
            ticket = next(iter(tickets_by_member[member]))
        key = ('ticket', ticket) if ticket else ('member', member)
        group = groups.setdefault(key, {'ticket_channel_id': ticket,
            'applicant_member_id': member, 'acceptance_decision': None,
            'finalized': None, 'timeline': []})
        if member:
            group['applicant_member_id'] = member
        group['timeline'].append(event)
    for group in groups.values():
        if dated:
            known = [event for event in group['timeline'] if event['event_ts'] is not None]
            unknown = [event for event in group['timeline'] if event['event_ts'] is None]
            group.update(period_evidence=known, undated_evidence=unknown,
                         period_status='matched' if known else 'unknown_date')
    return sorted(groups.values(), key=lambda row: (row['ticket_channel_id'] is None,
        row['ticket_channel_id'] or 0, row['applicant_member_id'] or 0))
