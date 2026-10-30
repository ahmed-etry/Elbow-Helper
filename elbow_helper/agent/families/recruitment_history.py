"""Read authorized recruitment history; interpret only this bot's typed embeds."""
from datetime import datetime, timezone

from elbow_helper.configuration.channels import TRIAL_LIST, TICKETS_LOG
from elbow_helper.configuration.clans import CLANS
from ..research.history import read_discord_channel_history, search_date


def mention_id(value, prefix):
    """Decode the leading Discord identifier in a bot-generated embed field."""
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    identifier, end, _ = value[len(prefix):].partition('>')
    return int(identifier) if end and identifier.isascii() and identifier.isdecimal() else None


def trial_record(message, bot_id):
    """Never classify member prose, copied embeds or role display names."""
    if bot_id is None or message.get('author_id') != bot_id:
        return None
    for embed in message.get('embeds', ()):
        if embed.get('title') != 'Active Trial':
            continue
        fields = {field.get('name'): field.get('value') for field in embed.get('fields', ())}
        applicant = mention_id(fields.get('Applicant'), '<@')
        ticket = mention_id(fields.get('Ticket'), '<#')
        started = fields.get('Started', '')
        clan_field = fields.get('Clans')
        if (not applicant or not ticket or not isinstance(started, str)
                or not started.startswith('<t:') or not started.endswith(':R>')
                or not isinstance(clan_field, str)):
            continue
        try:
            date = datetime.fromtimestamp(int(started[3:-3]), timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        role_ids = {role for value in clan_field.split(', ')
                    if (role := mention_id(value, '<@&')) is not None}
        clans = [clan.code for clan in CLANS.values() if not clan.is_utility
                 and clan.member_role_id in role_ids]
        participation = [clan.code for clan in CLANS.values() if clan.is_utility
                         and clan.member_role_id in role_ids]
        mapped = {clan.member_role_id for clan in CLANS.values()}
        placement_known = clan_field != 'Unknown' and role_ids <= mapped
        return {**message, 'applicant_member_id': applicant, 'member_id': applicant,
                'ticket_channel_id': ticket, 'start_at': date.isoformat(),
                'accepted_clan_codes': clans if placement_known else None,
                'participation_clan_codes': participation,
                'acceptance_decision': 'accepted_for_trial', 'finalized': None,
                'placement_basis': 'Clans field on the bot-authored trial-start tracking post',
                'workflow_status': 'trial_started'}
    return None


async def recruitment_history(context, arguments):
    """Use normal history cursors, destination scope and live permission checks."""
    stage = arguments['stage']
    try:
        after = search_date(arguments.get('after'))
        before = search_date(arguments.get('before')) or context.source_message.created_at
        if after is None or after >= before:
            raise ValueError
    except (ValueError, TypeError):
        return {'error': 'Choose a dated recruitment history window.', 'cause': 'invalid_arguments',
                **({'missing_fields': ['after']} if not arguments.get('after') else
                   {'invalid_fields': ['after', 'before']})}
    channel = TRIAL_LIST if stage == 'acceptance_history' else TICKETS_LOG
    result = await read_discord_channel_history(context, {
        'channel_id': channel, 'after': after.isoformat(), 'before': before.isoformat(),
        'limit': 100, **({'cursor': arguments['source_cursor']} if arguments.get('source_cursor') else {}),
    }, complete_content=True)
    if 'error' in result:
        return result
    bot_id = getattr(getattr(context.bot, 'user', None), 'id', None)
    rows = []
    for message in result.pop('messages', []):
        record = trial_record(message, bot_id) if stage == 'acceptance_history' else None
        if record is not None:
            if not after <= datetime.fromisoformat(record['start_at']) < before:
                continue
            if arguments.get('member_ids') and record['member_id'] not in arguments['member_ids']:
                continue
            rows.append(record)
        else:
            # Transcript text and decisions remain evidence for the model. No
            # keyword rules turn names, ticket status or member prose into outcomes.
            rows.append({**message, 'acceptance_decision': None, 'finalized': None})
    return {**result, 'rows': rows, 'stage': stage,
            'source_next_cursor': result.get('coverage', {}).get('next_cursor'),
            'history_complete': False,
            'workflow_meaning': 'Tracking posts record trial-start placement, not final membership. '
                'Read ticket-log transcripts for application decisions; a closed ticket alone is not a decline.'}
