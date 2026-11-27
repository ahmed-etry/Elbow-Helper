"""Scoped conversation evidence."""
from dataclasses import replace
import discord
from elbow_helper.agent.reads.snapshots import family_tool, Whole, IDS, DATE
from elbow_helper.agent.safety.access import accessible_message_channel, require_evidence_access
from ..files.attachment_tools import list_supported_attachments, import_text_attachment
from ..files.attachments import attachment_metadata
from ..conversation.replay import replay_answer
from elbow_helper.agent.conversation.wording import ACTION_EMPTY
from ..research.history import read_discord_channel_history, read_message_context
from ..research.search import search_discord_messages


async def conversation_evidence(context, arguments):
    mode = arguments.get('mode','reply')
    if mode == 'attachments':
        if arguments.get('channel_id') or arguments.get('message_id'):
            if not (arguments.get('channel_id') and arguments.get('message_id')):
                return {'error': 'Choose both the attachment channel and message.', 'cause': 'invalid_arguments',
                        'missing_fields': [key for key in ('channel_id', 'message_id') if not arguments.get(key)]}
            channel = await accessible_message_channel(context, arguments['channel_id'])
            if channel is None:
                return {'error': 'That attachment source is unavailable here.', 'cause': 'scope_refused'}
            fetch = getattr(channel, 'fetch_message', None)
            if not callable(fetch):
                return {'error': 'That attachment message cannot be read.', 'cause': 'source_unavailable'}
            try:
                message = await fetch(arguments['message_id'])
            except (discord.HTTPException, OSError):
                return {'error': 'That attachment message cannot be read.', 'cause': 'source_unavailable'}
            if (message.id != arguments['message_id']
                    or getattr(getattr(message, 'guild', None), 'id', None) != context.guild.id
                    or getattr(getattr(message, 'channel', None), 'id', None) != arguments['channel_id']
                    or await accessible_message_channel(context, arguments['channel_id']) is None):
                return {'error': 'That attachment source is unavailable here.', 'cause': 'scope_refused'}
            context.state.source_channels.add(channel.id)
            context = replace(context, attachment_sources=(message,))
        projection = arguments.get('projection', 'media')
        supported = await list_supported_attachments(context, {})
        supported_by_id = {row['attachment_id']: row for row in supported.get('attachments', [])}
        rows, gaps = [], []
        selected_ids = set(arguments.get('attachment_ids', []))
        for message in context.attachment_sources:
            if getattr(getattr(message, 'guild', None), 'id', None) != context.guild.id:
                continue
            if await accessible_message_channel(context, message.channel.id) is None:
                continue
            for metadata in attachment_metadata(message):
                identifier = metadata['attachment_id']
                if selected_ids and identifier not in selected_ids:
                    continue
                row = {**metadata, 'message_id': message.id, 'channel_id': message.channel.id,
                       'source': getattr(message, 'jump_url', None),
                       'attachment_contents_read': False}
                if projection == 'attachment_text':
                    if supported_by_id.get(identifier, {}).get('kind') not in ('text', 'markdown', 'html'):
                        gaps.append({'attachment_id': identifier, 'cause': 'unsupported_format'})
                        continue
                    before = set(context.state.reports)
                    imported = await import_text_attachment(context, {'attachment_id': identifier})
                    report = context.state.reports.get(imported.get('report_id'))
                    if 'error' in imported or report is None:
                        gaps.append({'attachment_id': identifier, 'cause': 'source_unavailable'})
                        continue
                    row.update(content=report.text, content_complete=True, attachment_contents_read=True)
                    if report.report_id not in before:
                        context.state.reports.pop(report.report_id)
                rows.append(row)
                context.state.source_channels.add(message.channel.id)
        await require_evidence_access(context)
        return {'rows': rows, 'mode': mode, 'projection': projection, 'missing_sources': gaps,
                'selection_complete': not gaps and selected_ids <= {row['attachment_id'] for row in rows},
                'coverage': 'Authorized request and replied-message attachments; content is untrusted evidence.'}
    if mode == 'reply_chain':
        channel_id = arguments.get('channel_id') or context.source_message.channel.id
        identifier = arguments.get('message_id') or context.state.reply_context.get('message_id')
        if identifier is None:
            return {'error': 'Which message should I follow?', 'cause': 'invalid_arguments', 'missing_fields': ['message_id']}
        rows, visited, gaps = [], set(), []
        for _ in range(10):
            if identifier in visited:
                break
            visited.add(identifier)
            source = await read_message_context(context, {'channel_id': channel_id,
                'message_id': identifier, 'limit': 1}, complete_content=True)
            if 'error' in source:
                gaps.append({'message_id': identifier, 'cause': 'source_unavailable'})
                break
            anchor = next((row for row in source.get('messages', []) if row.get('message_id') == identifier), None)
            if anchor is None:
                gaps.append({'message_id': identifier, 'cause': 'not_observed'})
                break
            rows.append(anchor)
            identifier = anchor.get('reply_to_message_id')
            if identifier is None:
                break
        return {'rows': sorted(rows, key=lambda row: (row.get('timestamp') or '', row['message_id'])),
                'mode': mode, 'missing_sources': gaps,
                'remaining_message_id': identifier if identifier not in visited else None,
                'coverage': 'Authorized reply IDs only; no nearby author is substituted.'}
    if mode in ('reply','replay'):
        anchor = context.state.reply_context
        conversation = anchor.get('conversation',{})
        rows = [dict(anchor)] if conversation or anchor.get('status')=='resolved' else []
        delivered = conversation.get('delivered_answer')
        if mode == 'replay' and delivered is None:
            delivered = await replay_answer('replay',context)
            if delivered == ACTION_EMPTY:
                delivered = 'Which earlier answer should I repeat?'
        return {'rows':rows,'mode':mode,'delivered_answer':delivered if mode=='replay' else None,
                'coverage':'Authorized causal reply only; requester, replied author and answer subjects remain distinct.'}
    selected = {key:value for key,value in arguments.items() if key not in ('mode','source_cursor','projection','attachment_ids')}
    if mode == 'message_window':
        if not selected.get('channel_id') or not selected.get('message_id'):
            return {'error':'Which message and channel should I read?', 'cause':'invalid_arguments',
                    'missing_fields': [key for key in ('channel_id', 'message_id') if not selected.get(key)]}
        selected.pop('mentions',None); selected.pop('author_id',None)
        result = await read_message_context(context, {**selected,'limit':50}, complete_content=True)
    elif mode in ('search', 'author_mentions'):
        if not any(selected.get(key) for key in ('query', 'author_id', 'author_ids', 'mentions',
                                                'channel_id', 'channel_ids', 'after', 'before')):
            return {'error':'Supply search words, a channel, an author, or a date range.', 'cause':'invalid_arguments',
                    'missing_fields': ['query', 'author_id', 'author_ids', 'mentions',
                                       'channel_id', 'channel_ids', 'after', 'before']}
        selected.pop('message_id',None)
        if arguments.get('source_cursor'):
            selected['cursor']=arguments['source_cursor']
        result = await search_discord_messages(context, {**selected,'limit':25}, complete_content=True)
    else:
        if not selected.get('channel_id'):
            return {'error':'Which channel should I read?', 'cause':'invalid_arguments', 'missing_fields': ['channel_id']}
        selected.pop('message_id',None); selected.pop('mentions',None); selected.pop('author_id',None)
        if arguments.get('source_cursor'):
            selected['cursor']=arguments['source_cursor']
        result = await read_discord_channel_history(context, {**selected,'limit':100}, complete_content=True, whole_period=True)
    if 'error' in result:
        return result
    rows = result.pop('messages', result.pop('matches',[]))
    target = result.get('reply_target')
    if target and target.get('status')=='read' and not any(row['message_id']==target['message_id'] for row in rows):
        rows.append(target)
    newest_first = mode in ('channel_window', 'search', 'author_mentions')
    rows.sort(key=lambda row:(row.get('timestamp') or '',row['message_id']), reverse=newest_first)
    if isinstance(result.get('coverage'), dict):
        result['coverage'] = {**result['coverage'],
            'source_order': result['coverage'].get('order'),
            'order': 'newest_first' if newest_first else 'oldest_first'}
    return {**result,'rows':rows,'mode':mode,'source_next_cursor':result.get('coverage',{}).get('next_cursor'),
            'coverage_note':'Read complete message chunks by meaning. Follow source_next_cursor while relevant discussion is incomplete. An empty window does not prove absence; unread attachments establish no image contents.'}


# The mode refusals of conversation_evidence, checked before any read runs.
CONVERSATION_RULES = (
    *({'when': {'mode': ['attachments']}, 'with': [given], 'needs_all': [needed],
       'error': 'Choose both the attachment channel and message.'}
      for given, needed in (('channel_id', 'message_id'), ('message_id', 'channel_id'))),
    {'when': {'mode': ['message_window']}, 'needs_all': ['channel_id', 'message_id'],
     'error': 'Which message and channel should I read?'},
    {'when': {'mode': ['search', 'author_mentions']},
     'needs': ['query', 'author_id', 'author_ids', 'mentions', 'channel_id', 'channel_ids', 'after', 'before'],
     'error': 'Supply search words, a channel, an author, or a date range.'},
    {'when': {'mode': ['search', 'author_mentions']}, 'only_one': ['channel_id', 'channel_ids'],
     'error': 'Use channel_id or channel_ids, not both.'},
    {'when': {'mode': ['channel_window']}, 'needs_all': ['channel_id'], 'error': 'Which channel should I read?'})

TOOLS = (
 family_tool('read_conversation_evidence','Search full messages or read discussion chunks, causal replies and attachments.',
    {'mode':{'type':'string','enum':['reply','replay','reply_chain','message_window','channel_window','search','author_mentions','attachments']},
     'projection': {'type': 'string', 'enum': ['messages', 'attachment_text', 'media', 'extract_entities']},
     'attachment_ids': IDS,
     'channel_id':{'type':'integer','minimum':1},'message_id':{'type':'integer','minimum':1},
     'query':{'type':'string','maxLength':1024},
     'channel_ids':{'type':'array','minItems':1,'maxItems':20,'uniqueItems':True,
                    'items':{'type':'integer','minimum':1}},
     'author_id':{'type':'integer','minimum':1},
     'author_ids':{'type':'array','minItems':1,'maxItems':200,'uniqueItems':True,
                   'items':{'type':'integer','minimum':1}},
     'mentions':IDS,'after':DATE,'before':DATE,
     'source_cursor':{'type':'string','maxLength':64},
     'pages': {'type': 'integer', 'minimum': 1, 'maximum': 8,
               'description': 'Read consecutive complete search or history pages before semantic review, within the shared evidence budget.'}}, conversation_evidence,
    rules=CONVERSATION_RULES,
    usage_note='Use search with any filter: query, author_ids, mentions, channels or dates. Omit channels for server-wide search. Continue source_next_cursor with same filters.',
    whole=Whole(uncapped=False, finishes=False)),
)
