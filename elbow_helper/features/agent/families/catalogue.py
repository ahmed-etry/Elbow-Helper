"""Explicit specialist discovery; no classification of request prose."""
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..models import RegisteredAgentTool, AgentCapabilityEffect
from ..actions.contracts import ActionClass
from ..access import has_lookup_access
from ..engine.capability_contract import CapabilityContract
from ..plan.format import capability_list

GROUPS = ('changes', 'schedules', 'attachments', 'research_jobs', 'events',
          'achievements', 'transfers', 'settings', 'records', 'action_history')

FAMILY_INSTRUCTION = (
    'Use the visible typed family reads for routine facts. Their handlers select sources, '
    'join identities and compute complete results; do not reproduce those operations with '
    'SQL or propose capabilities absent from the current checked catalogue. Plan independent '
    'families together and use typed result references for dependent reads. '
    'For a selected account cohort, use its cohort_ref for dependent measures so historical '
    'participants do not replace the requested population. Open a specialist '
    'group explicitly only for work the family reads do not cover. Opening a group performs '
    'no action and grants no permission. Normal family work uses one plan and one answer; '
    'reserve another round for a real missing field or source. Set finish_after_reads=true '
    'for simple factual requests when this plan covers all required sources, including '
    'dependent reads. Leave false when results must select a later read or action. '
    'Omit page limits for full '
    'results; top_n is an explicit selection. Retain full snapshots internally; attach a '
    'complete export only when the member requests a file or the requested output rows '
    'cannot fit a message. Evidence size alone is not a delivery request. '
    'Distinguish a complete snapshot of checked evidence from complete historical collection. '
    'Preserve supported rows and precise gaps when another source or export fails. '
    'For a clan directory use read_clan_rosters view=count for its per-clan current names, '
    'counts and capacity, and read_cwl_results metric=league without historical selectors '
    'for current CWL labels. Summaries remain available when account rows are exported. '
    'Use read_bot_command_help to verify this bot\'s registered commands, options and trial '
    'controls. Community policies do not enumerate all implemented commands. A missing '
    'command in a policy section does not establish that the command is absent. '
    'For how this bot works, read_community_reference reference_kind=bot_workflows provides '
    'approved code-derived ASS formulas and profile settings, ticket opening and automatic '
    'welcome behavior, requester ticket lookup, recruit placement versus promotion, '
    'roster signup management with account removal controls and their required roles, '
    'and automatic jobs including cleanup schedules and protected messages. '
    'Use this implementation reference alongside policy; historical narrative or workbook '
    'settings do not override current implementation. For a requester\'s recruitment ticket '
    'use read_recruitment_pipeline stage=requester_tickets; no department-wide read is needed. '
    'The sourced community_data context describes record lifetimes and family read contents. '
    'Select sources whose coverage spans the requested period; follow source cursors and '
    'read relevant transcript attachments. Reconcile records by verified member/account/ticket '
    'identities. Distinguish observed decisions from current membership and derived placement. '
    'For an informal responsibility, use knowledge to identify the person or responsible '
    'role, then check live holders when needed. Lead with the person; do not turn an '
    'informal title into an unasked role-existence question. '
    'For a named responsibility use read_member_cohort basis=role_holders with the exact '
    'role_name, such as "War Co"; add clan_codes to intersect those holders with clan home '
    'roles. Do not substitute clan_role=leadership for a different responsibility. '
    'clan_role selects configured roles only; war is the war participant role, not War Co. '
    'Knowledge explains responsibilities; in-game roster ranks do not identify Discord assignments. '
    'For an explicitly requested guild role, use find_discord_roles to check existence and '
    'resolve IDs, then read_member_cohort basis=role_holders with those role_ids for all holders. '
    'This covers every guild role, including department and Town Hall roles. A policy '
    'describing a responsibility is not a live role or holder lookup. Follow role lookup pages '
    'when needed; a missing policy entry does not establish that a guild role is absent. '
    'Your capabilities include family reads, confirmed Discord posts and reminders. '
    'Use save_standing_rule for personal reminders; ask for a missing time or timezone. '
    'Open schedules for other schedule controls and changes for other supported actions '
    'before claiming an ability is absent. An addressed-to-another-member request still '
    'gets useful available reads now; do not merely offer to fetch the requested data. '
    'For discussion questions use read_conversation_evidence mode=channel_window. Read full '
    'chunks and choose relevant messages by meaning, including different wording and follow-ups. '
    'Omit after when the start is unknown. Continue source_next_cursor in the same channel '
    'while the requested discussion is incomplete; a report snapshot is only one source chunk. '
    'Use mode=author_mentions only for a specifically requested author or mention set. '
    'For stats on named subjects, select game accounts with read_member_accounts account_names '
    'unless the request or established context explicitly selects Discord members. This resolves '
    'case, spacing and small misspellings across all family rosters and stored names without '
    'clan_codes, keeping ambiguous tags explicit and excluding unrelated alts. For activity '
    'over a requested period, include after/before on that same read: it reads stored regular '
    'war and CWL participation and ranked history for the exact resolved tags. The retained '
    'rows include per-account activity and history_reads records each source outcome. '
    'For member '
    'evaluation, select the requested Discord cohort first (for competitive members, the '
    'Competitive role), include_accounts=true, then join its report via cohort_ref to '
    'read_cwl_results for season performance/ASS and read_war_attendance for the dated '
    'regular-war window. ASS needs clan_codes; select all relevant CWL clans, not just '
    'the current home clan. Use read_member_cohort include_activity=true with after/before '
    'for that same cohort\'s donations, war and authorized chat metadata. Policy and raw '
    'roster TH alone cannot establish a member\'s skill or motivation. Retrieve the relevant '
    'measures before assessing fit; distinguish observed performance from inferred motivation. '
    'For missed attacks, including home-assignment or inactivity questions about players who '
    'signed up but did not attack, read home assignments and read_war_attendance. For the last '
    'completed regular war, use war_type=REG, latest_completed=1 and the relevant clan_codes; '
    'omit member_ids/account_tags to keep the whole war lineup. Reading only the asker\'s '
    'accounts cannot answer who missed attacks. Resolve the clan from the channel, context '
    'or home roles, then read participation before claiming that list is unavailable or sending '
    'the member to a command. Use read_current_war for attacks still due. '
    'For ASS or bonus, omit period selectors for the latest stored season, or select season '
    'explicitly. latest_completed counts individual wars, never seasons; use it only for a '
    'requested war subset. Stored war opportunities describe collected account-war participation, not all wars '
    'someone could have joined. Historical home assignments need dated evidence; current '
    'roles do not establish last month\'s home. '
    'For home clan, read_member_accounts and read_member_cohort expose home_clans from '
    'configured Discord clan member roles. Use those roles, not an account\'s in-game clan, '
    'CWL assignment or roster rank. Multiple home roles remain explicit; do not choose a '
    'single home without evidence; linked accounts\' current clans can clarify multiple roles. '
    'home_clans.role_name and roles.clan_purposes tie the '
    'configured clan role to its current display name. They describe the same role ID, '
    'not separate requirements. read_community_reference reference_kind=clans provides '
    'configured purpose/ID/current-name bindings for all clan role types; a display name '
    'alone does not establish a role\'s purpose or whether the member holds it. '
    'home_clans excludes utility-clan member roles: BEW is extra-wars participation '
    '(War Specialist), never recruit placement. participation_clans stays separate. '
    'Never infer a role\'s meaning, clan scope or exclusivity from its display name, '
    'including Elder BEH. For Discord elder-role questions read approved knowledge and configured '
    'clan role bindings alongside live roles. If no source defines the role, state only '
    'the observed name and membership, leave its meaning unknown, and direct the member '
    'to verified clan leadership or server role staff. An in-game elder rank does not '
    'establish a corresponding Discord assignment. '
    'A knowledge index is a locator, not a failed read. Open the relevant indexed section '
    'before saying a guide is unavailable. For war timing and claims, establish the channel\'s '
    'clan from current_channel and the channel map before considering the asker\'s home roles. '
    'Unless the request names another clan, read that channel clan\'s rules from the current '
    'index or linked/pinned instructions first. Clan rules take priority over family guidance. '
    'If the clan rule cannot be read, say it is unavailable here; never substitute general '
    'guidance or claim no timing rule exists. A target reservation is not necessarily a '
    'delay between attacks. A malformed or rejected plan ran no reads and '
    'does not establish an access restriction. For an actual scoped exclusion, use the '
    'returned access_limits and available_privately flag to give the verified reader roles '
    'or the supported private-check signal, without revealing excluded titles or bodies. '
    'When a question is addressed to another member, add only a useful verified contribution. '
    'For a cohort count, read the named role holders intersected with the requested home '
    'clan. Never send an empty interjection that merely follows the question or expresses '
    'curiosity about its answer. '
    'For cohort-wide war, donation or interaction questions, read_member_cohort basis=clan_rosters '
    'with include_activity=true and a dated after/before window joins all selected roster accounts '
    'to their owners, donations, stored war participation and recent authorized chat metadata. '
    'Apply population filters in the cohort read, including exclude_leadership when needed, '
    'then pass cohort_ref to dependent measure reads in the same plan. This cohort already '
    'contains current account TH, donations and clan capacity; avoid repeating those reads '
    'or leaving population filters to prose. '
    'Omit clan_codes for the whole family. Never substitute the asker\'s linked accounts for '
    'a requested member cohort. Keep all rows through report cursors; missing links, collection '
    'gaps and no observed messages remain unknown rather than proving inactivity. '
    'For large activity cohorts, use view=count while collecting. Before synthesis the server '
    'resumes incomplete activity_ref channel cursors within the request budgets and supplies '
    'the authorized retained member rows to the writer. A count-only transport response does '
    'not mean the roster is empty. Preserve any remaining chat coverage gaps. '
    'For accounts versus Discord roles, combine a roster snapshot, read_member_accounts '
    'cohort_ref with include_roles=true, and read_member_cohort clan_role=member for the '
    'configured home role IDs; clan_role=member selects home roles. Keep unlinked and departed owners unknown rather than treating '
    'them as confirmed missing-role members. Compare current role permissions and room access '
    'with read_discord_access alongside any relevant policy reference. A role comparison about '
    'this room needs both the live access read and the relevant policy in the same plan: '
    'read_discord_access role_names=["Co-Leader","Cross-Leader"], omitting channel_ids. '
    'Check role_change_access before planning role changes. If requester_allowed=false, '
    'state the required permission and current operator role names; do not plan an add/remove. '
    'A read-only eligibility result is never an attempted change, including when denied. '
    'Existing members link additional accounts through /profile; consult bot_workflows '
    'alongside the relevant account policy. get-started is the new application route. '
    'For blocked in-game join requests or clan entry settings, always read the approved '
    'FAQ section "Why can I not request to rejoin after being rejected or kicked?" '
    'and read_clan_rosters basis=current_in_game view=count for the destination in the same '
    'plan. clans.entry_settings gives live type and required TH/trophies. Ask leadership '
    'for an in-game invitation or backlog check as the FAQ explains. Do not map in-game '
    'wording such as transfer request to Discord /transfer without command-specific evidence, '
    'and do not bring up that command in an in-game entry answer. Omit Discord queue '
    'availability and seasonal command restrictions from in-game entry help, even when '
    'they appear in a retrieved reference. Include them only for an explicitly established '
    'Discord queue or command question. For an actual Discord '
    '/transfer request question, bot_workflows gives cwl_season_only per destination from '
    'the owning code; never apply the seasonal limit to other destinations such as BEH. '
    'A reported banned rejoin notice does not prove a leadership-managed clan ban list; '
    'do not invent that game mechanic. Read that same approved FAQ and relevant conversation '
    'for a recent kick, rejection or cleanup. State a likely cause as likely, then use the '
    'supported invitation route; do not invent a diagnosis or a ban-removal workflow. '
    'Role add/remove capabilities prepare checked confirmation previews directly by exact '
    'role_name or role_id; they do not apply changes. Use the read-only role delta capability '
    'when only inspection is requested. These visible paths need no specialist discovery.'
)


def group_for(owner, tool):
    name = tool.definition.name
    if name == 'read_discord_role_change_preview':
        return 'changes'
    if owner == 'standing_rules':
        return 'schedules'
    if owner == 'files' or tool.effect is AgentCapabilityEffect.ARTIFACT:
        return 'attachments'
    if owner in ('events', 'achievements'):
        return owner
    if owner == 'clan_transfers':
        return 'transfers'
    if name in ('read_agent_action_log', 'undo_agent_action'):
        return 'action_history'
    if 'settings' in name or 'config' in name:
        return 'settings'
    if tool.action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
        return 'changes'
    if owner == 'discord_research':
        return 'research_jobs'
    return 'records'


def discovery_tool(available, active, on_expand):
    async def open_group(context, arguments):
        group = arguments['group']
        if group not in GROUPS:
            return {'error': 'Choose a listed capability group.', 'cause': 'invalid_arguments'}
        cursor = arguments.get('cursor', '0')
        if not isinstance(cursor, str) or not cursor.isdecimal():
            return {'error': 'Use the returned catalogue cursor.', 'cause': 'invalid_arguments'}
        selected = {name: tool for name, tool in sorted(available.items())
                    if name != 'query_bot_data' and tool.specialist_group == group
                    and (arguments['intent'] == 'prepare_change'
                         or tool.action_class in (ActionClass.READ, ActionClass.OUTPUT))
                    and has_lookup_access(context, tool.contract.required_access if tool.contract else frozenset())}
        role_changes = ('read_discord_role_change_preview', 'add_discord_roles', 'remove_discord_roles')
        names = sorted(selected, key=lambda name: (name not in role_changes, name)) if group == 'changes' else list(selected)
        offset = int(cursor)
        if offset > len(names):
            return {'error': 'That catalogue cursor is unavailable.', 'cause': 'invalid_arguments'}
        role_page = group == 'changes' and offset == 0 and any(name in selected for name in role_changes)
        page_end = sum(name in selected for name in role_changes) if role_page else offset + 12
        page = {name: selected[name] for name in names[offset:page_end]}
        if group == 'changes' and offset == 0:
            # Role discovery and its read-only preview must not depend on the
            # alphabetical location among unrelated change capabilities.
            for name in ('find_discord_roles', 'read_discord_members', 'read_discord_role_change_preview',
                         'add_discord_roles', 'remove_discord_roles'):
                tool = available.get(name)
                if tool is not None and (arguments['intent'] == 'prepare_change'
                        or tool.action_class in (ActionClass.READ, ActionClass.OUTPUT)) and has_lookup_access(
                            context, tool.contract.required_access if tool.contract else frozenset()):
                    page[name] = tool
        active.update(page)
        on_expand()
        return {'group': group, 'catalogue': capability_list(page),
                **({'roles': [{'role_id': role.id, 'name': role.name} for role in context.guild.roles
                              if not role.is_default()],
                    'role_preview_path': 'read_discord_role_change_preview'} if group == 'changes' and offset == 0 else {}),
                'total_capabilities': len(names),
                'next_cursor': str(page_end) if page_end < len(names) else None,
                'instruction': 'These capabilities are now available in the checked request plan. '
                               'Opening this group performed no action.'}
    return RegisteredAgentTool(AgentToolDefinition('open_capability_group',
        'Open specialist capabilities; changes starts with guild roles and read-only role delta previews.',
        {'type': 'object', 'properties': {
            'group': {'type': 'string', 'enum': list(GROUPS)},
            'intent': {'type': 'string', 'enum': ['inspect', 'prepare_change']},
            'cursor': {'type': 'string', 'maxLength': 10}},
         'required': ['group', 'intent'], 'additionalProperties': False}),
        open_group, effect=AgentCapabilityEffect.STATE, contract=CapabilityContract(),
        returns='catalogue,total_capabilities,next_cursor,roles:[{role_id,name}],role_preview_path')
