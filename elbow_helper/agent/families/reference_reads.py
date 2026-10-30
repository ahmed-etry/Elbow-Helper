"""Exact approved reference selections and scoped conversation evidence."""
import asyncio
from dataclasses import replace
import discord
from elbow_helper.agent.knowledge.store import VISIBILITIES
from .core import family_tool, IDS, DATE
from ..access import READ_ACCESS_ROLE_SETS, has_reader_access_requirements, require_lookup_access, accessible_message_channel, require_evidence_access
from ..files.attachment_tools import list_supported_attachments, import_text_attachment
from ..files.attachments import attachment_metadata
from .domain_facts import domain_reference
from .war_reads import CLANS_INPUT
from ..disclosure import request_scope, exclude_source
from ..knowledge.tools import _section_id
from ..conversation.replay import replay_answer
from ..wording import ACTION_EMPTY
from ..research.history import read_discord_channel_history, read_message_context
from ..research.search import search_discord_messages
from ..clan_roles import configured_clan_roles


async def community_reference(context, arguments):
    kind = arguments.get('reference_kind','knowledge')
    if kind == 'bot_workflows':
        from .bot_workflows import bot_workflows
        return bot_workflows(context)
    if kind != 'knowledge':
        result = domain_reference(kind, arguments.get('clan_codes'))
        if kind == 'clans':
            for row in result['rows']:
                row['roles'] = configured_clan_roles(context.guild, row['clan_code'])
        return result
    if context.knowledge_store is None:
        return {'error':'Community reference is not available.', 'cause':'missing_setup'}
    catalog = await asyncio.to_thread(context.knowledge_store.load)
    if catalog.store_status == 'unavailable':
        return {'error':'Community reference is not available.', 'cause':'source_unavailable'}
    scope = await request_scope(context)
    levels = scope.levels & (VISIBILITIES-{'public'})
    ids, titles = set(arguments.get('section_ids',[])), set(arguments.get('titles',[]))
    selection = bool(ids or titles)
    selected_headings = {(section.file_name, section.title, section.visibility)
                         for section in catalog.active_sections if _section_id(section) in ids}
    def selected(section):
        return ((section.file_name, section.title, section.visibility) in selected_headings
                or section.title in titles or section.file_name in titles)
    excluded_levels = set()
    for section in catalog.active_sections:
        if selection and selected(section) and section.visibility != 'public' and section.visibility not in levels:
            authorized = has_reader_access_requirements(context.guild, context.member.id, {section.visibility})
            if authorized:
                excluded_levels.add(section.visibility)
            exclude_source(context,'knowledge:'+_section_id(section),'role_scope',
                relevant=authorized)
    visible = [section for section in catalog.visible_sections(levels) if not selection or selected(section)]
    needed = {section.visibility for section in visible if section.visibility != 'public'}
    require_lookup_access(context, needed)
    context.state.required_access.update(needed)
    rows = []
    for section in visible:
        identifier = _section_id(section)
        rows.append({'section_id':identifier,'title':section.title,'file_name':section.file_name,
                     'visibility':section.visibility, 'part':section.part, 'parts':section.parts,
                     **({'applicability': section.applicability} if section.applicability else {}), **({'body':section.body} if selection else {})})
        if selection:
            context.state.knowledge_sources.add(identifier)
    # Exact approved section identities connect policy to its owning implementation.
    # No request or message prose is classified to decide which workflow applies.
    recruitment_sections = {
        ('leadership.md', 'Clan placement and skill questions: open a ticket'),
        ('recruitment-promotions.md', 'Clan promotion exams: BES to BE4, casual entry and Hellbow assessment'),
        ('recruitment-promotions.md', 'Trial: competitive, casual and starter placements'),
        ('recruitment-promotions.md', 'Joining in game: approval, join requests and a full clan'),
    }
    implementation = {}
    workflows_needed = set()
    if selection and any((section.file_name, section.title) in recruitment_sections for section in visible):
        workflows_needed.add('recruit_placement')
    account_sections = {
        ('faq.md', 'Adding a village and leaving an unused account linked'),
        ('faq.md', 'Joining tickets and adding alts to the family'),
        ('faq.md', 'Can I bring a second account or a strategically rushed alt?'),
    }
    if selection and any((section.file_name, section.title) in account_sections for section in visible):
        workflows_needed.add('account_linking')
    if workflows_needed:
        from .bot_workflows import bot_workflows
        workflows = bot_workflows()
        implementation['implementation_reference'] = {
            **workflows, 'rows': [row for row in workflows['rows'] if row['workflow'] in workflows_needed]}
    return {'rows':sorted(rows,key=lambda row:(row['file_name'],row['title'],row['part'],row['section_id'])),
            **implementation,
            'mode':'sections' if selection else 'index','store_status':catalog.store_status,
            'available_privately':bool(excluded_levels),
            'access_limits': [{'reader_roles': [
                {'role_id': role.id, 'name': role.name} for role in context.guild.roles
                if role.id in READ_ACCESS_ROLE_SETS[level] and getattr(role, 'name', None)],
                'available_privately': True} for level in sorted(excluded_levels)],
            'selection_complete':catalog.store_status=='available' and (
                not selection or bool(visible) and ids <= {_section_id(section) for section in visible}
                and all(any(section.title == title or section.file_name == title for section in visible) for title in titles)),
            'coverage':'Exact approved titles or section IDs. Index rows contain no section bodies.'}


async def conversation_evidence(context, arguments):
    mode = arguments.get('mode','reply')
    if mode == 'attachments':
        if arguments.get('channel_id') or arguments.get('message_id'):
            if not (arguments.get('channel_id') and arguments.get('message_id')):
                return {'error': 'Choose both the attachment channel and message.', 'cause': 'needs_input'}
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
            return {'error': 'Which message should I follow?', 'cause': 'needs_input', 'missing_fields': ['message_id']}
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
            return {'error':'Which message and channel should I read?', 'cause':'needs_input'}
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
            return {'error':'Which channel should I read?', 'cause':'needs_input'}
        selected.pop('message_id',None); selected.pop('mentions',None); selected.pop('author_id',None)
        if arguments.get('source_cursor'):
            selected['cursor']=arguments['source_cursor']
        result = await read_discord_channel_history(context, {**selected,'limit':100}, complete_content=True)
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


TOOLS = (
 family_tool('read_community_reference','Read sourced clan, role and workflow facts, approved headings or complete reference sections.',
    {'reference_kind':{'type':'string','enum':['knowledge','clans','roles','workflows','bot_workflows']},
     'clan_codes':CLANS_INPUT,
     'titles':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'string','maxLength':200}},
     'section_ids':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'string','minLength':64,'maxLength':64}}}, community_reference),
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
    usage_note='Use search with any filter: query, author_ids, mentions, channels or dates. Omit channels for server-wide search. Continue source_next_cursor with same filters.'),
)
