"""Bind capability reads to structured entity, time, and access fields."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Any, Mapping

from elbow_helper.domain.player_tags import normalize_player_tag

from .access import ACCESS_LEAD, ACCESS_LEAD_PLUS, KNOWN_ACCESS_REQUIREMENTS
from .models import RegisteredAgentTool


class CapabilityBindError(ValueError):
    """A structurally valid call does not identify a supported data scope."""


MECHANICAL_FIELDS = frozenset({
    "offset", "limit", "cursor", "page_size", "expected_version",
    "expected_state_fingerprint", "title", "sheets", "report_sheets",
    "written_sheets", "label", "quote", "section_offset",
    "section_limit", "content_offset", "content_limit", "ticket_offset",
    "winner_offset", "query", "report_kind",
})

ENTITY_KINDS = {
    "source_clan": "clan", "destination_clan": "clan",
    "current_clan": "clan", "discord_channel": "discord_channel",
    "parent_discord_channel": "discord_channel",
    "examination_ticket_channel": "discord_channel",
    "recruitment_ticket_channel": "discord_channel",
    "support_ticket_channel": "discord_channel",
}


def entity_kind(kind: str) -> str:
    if kind.endswith("_set"):
        kind = kind[:-4]
    return ENTITY_KINDS.get(kind, kind)


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    entity_fields: tuple[tuple[str, str], ...]
    time_fields: tuple[str, ...]
    scope_field: str | None = None
    scope_variants: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source_scope: str = "other"
    channel_fields: tuple[str, ...] = ()
    result_channel_lists: tuple[tuple[str, str], ...] = ()
    result_channel_fields: tuple[str, ...] = ()
    result_sources_within_query: bool = False
    filter_fields: tuple[str, ...] = ()
    result_entity_keys: tuple[tuple[str, str], ...] = ()
    time_window: tuple[str, str, str] | None = None
    required_access: frozenset[str] = frozenset()
    latest_fields: tuple[str, ...] = ()
    bounded_fields: tuple[str, ...] = ()
    period_results: tuple[tuple[str | int, ...], ...] = ()
    value_patterns: tuple[tuple[str, str], ...] = ()
    retained_fields: tuple[str, ...] = ()

    def catalogue_entry(self) -> dict[str, Any]:
        return {
            "entity_keys": dict(self.entity_fields),
            "time_fields": self.time_fields,
            "required_access": tuple(sorted(self.required_access)),
            "scope_variants": dict(self.scope_variants),
            "source_scope": self.source_scope,
            "channel_fields": self.channel_fields,
            "result_channel_lists": self.result_channel_lists,
            "result_channel_fields": self.result_channel_fields,
            "result_sources_within_query": self.result_sources_within_query,
            "filter_fields": self.filter_fields,
            "result_entity_keys": dict(self.result_entity_keys),
            "time_window": self.time_window,
            "latest_fields": self.latest_fields,
            "bounded_fields": self.bounded_fields,
            "period_results": self.period_results,
        }


CONTRACTS: Mapping[str, CapabilityContract] = {
    "read_bot_command_help": CapabilityContract((), ()),
    "find_discord_channels": CapabilityContract((), (), source_scope='channel_locator'),
    "search_discord_messages": CapabilityContract((('channel_id', 'discord_channel'), ('channel_ids', 'discord_channel_set'), ('author_id', 'discord_member')), ('after', 'before', 'cursor'), source_scope='channel_messages', channel_fields=('channel_id', 'channel_ids'), result_channel_lists=(('matches', 'channel_id'),), result_sources_within_query=True, time_window=('after', 'before', 'iso_utc')),
    "read_discord_channel_history": CapabilityContract((('channel_id', 'discord_channel'), ('author_id', 'discord_member')), ('after', 'before', 'cursor'), source_scope='channel_messages', channel_fields=('channel_id',), result_channel_lists=(('messages', 'channel_id'),), result_sources_within_query=True, time_window=('after', 'before', 'iso_utc')),
    "read_message_context": CapabilityContract((('channel_id', 'discord_channel'), ('message_id', 'discord_message')), ('after', 'before'), source_scope='channel_messages', channel_fields=('channel_id',), result_channel_fields=('channel_id',), result_sources_within_query=True, time_window=('after', 'before', 'iso_utc')),
    "find_discord_threads": CapabilityContract((('parent_channel_id', 'discord_channel'),), ('cursor',), source_scope='channel_messages', channel_fields=('parent_channel_id',), result_channel_lists=(('threads', 'thread_id'),), filter_fields=('state', 'visibility')),
    "start_discord_research_job": CapabilityContract((('channel_id', 'discord_channel'), ('author_id', 'discord_member')), ('after', 'before'), source_scope='channel_messages', channel_fields=('channel_id',), result_channel_fields=('source_channel_id',), result_sources_within_query=True, time_window=('after', 'before', 'iso_utc')),
    "start_discord_history_job": CapabilityContract((('channel_id', 'discord_channel'), ('author_id', 'discord_member')), ('after', 'before'), source_scope='channel_messages', channel_fields=('channel_id',), result_channel_fields=('source_channel_id',), result_sources_within_query=True, time_window=('after', 'before', 'iso_utc')),
    "start_discord_research_batch": CapabilityContract((('channel_ids', 'discord_channel_set'), ('author_id', 'discord_member')), ('after', 'before'), source_scope='channel_messages', channel_fields=('channel_ids',), result_channel_lists=(('jobs', 'source_channel_id'),), result_sources_within_query=True, filter_fields=('kind',), time_window=('after', 'before', 'iso_utc')),
    "continue_discord_research_job": CapabilityContract((('job_id', 'discord_research_job'),), (), source_scope='retained_channel_evidence', result_channel_fields=('source_channel_id',), result_channel_lists=(('new_messages', 'channel_id'),)),
    "read_discord_research_job": CapabilityContract((('job_id', 'discord_research_job'),), (), source_scope='retained_channel_evidence', result_channel_fields=('source_channel_id',), result_channel_lists=(('messages', 'channel_id'),)),
    "retain_discord_research_report": CapabilityContract((('job_id', 'discord_research_job'),), (), source_scope='retained_channel_evidence', result_channel_fields=('source_channel_id',), result_channel_lists=(('messages', 'channel_id'),)),
    "read_discord_research_report": CapabilityContract((('report_id', 'discord_research_report'),), (), source_scope='retained_channel_evidence', result_channel_fields=('source_channel_id',), result_channel_lists=(('messages', 'channel_id'),)),
    "read_discord_research_jobs": CapabilityContract((('job_ids', 'discord_research_job_set'),), (), source_scope='retained_channel_evidence', result_channel_lists=(('jobs', 'source_channel_id'),)),
    "read_conversation_history": CapabilityContract((('request_message_id', 'discord_message'),), ()),
    "search_approved_knowledge": CapabilityContract((), (), filter_fields=('topics',)),
    "list_supported_attachments": CapabilityContract((), (), source_scope='request_attachment', result_channel_lists=(('attachments', 'channel_id'),)),
    "import_csv_attachment": CapabilityContract((('attachment_id', 'discord_attachment'),), (), source_scope='request_attachment', result_channel_fields=('source_channel_id',), filter_fields=('delimiter',)),
    "import_xlsx_attachment": CapabilityContract((('attachment_id', 'discord_attachment'),), (), source_scope='request_attachment', result_channel_fields=('source_channel_id',)),
    "import_text_attachment": CapabilityContract((('attachment_id', 'discord_attachment'),), (), source_scope='request_attachment', result_channel_fields=('source_channel_id',)),
    "get_linked_accounts": CapabilityContract((('member_id', 'discord_member'),), (), result_entity_keys=(('member_id', 'discord_member'), ('accounts[].player_tag', 'clash_account'))),
    "get_account_link": CapabilityContract((('player_tag', 'clash_account'),), (), result_entity_keys=(('player_tag', 'clash_account'), ('linked_member_id', 'discord_member'))),
    "find_discord_members": CapabilityContract((), ()),
    "find_discord_roles": CapabilityContract((), ()),
    "audit_role_accounts": CapabilityContract((('role_ids', 'discord_role_set'),), (), filter_fields=('refresh_locations',)),
    "refresh_role_account_report": CapabilityContract((('report_id', 'role_account_report'), ('player_tags', 'clash_account_set')), ()),
    "read_role_account_report": CapabilityContract((('report_id', 'role_account_report'), ('clan_code', 'clan'), ('member_id', 'discord_member')), (), filter_fields=('selection',)),
    "compare_role_account_reports": CapabilityContract((('before_report_id', 'role_account_report'), ('after_report_id', 'role_account_report')), ()),
    "read_role_connections": CapabilityContract((('member_id', 'discord_member'),), (), required_access=frozenset({ACCESS_LEAD})),
    "read_cwl_performance": CapabilityContract((('clan_code', 'clan'), ('player_tag', 'clash_account')), ('season', 'history_limit'), result_entity_keys=(('players[].player_tag', 'clash_account'), ('players[].season', 'cwl_season'), ('players[].clan_code', 'clan'))),
    "list_cwl_ass_seasons": CapabilityContract((('clan_code', 'clan'),), (), result_entity_keys=(('season_coverage[].season', 'cwl_season'), ('clan_code', 'clan'))),
    "read_cwl_ass_scope": CapabilityContract((('clan_code', 'clan'),), ('season', 'scope_type', 'cwl_round', 'war_id'), 'scope_type', (('season', ()), ('round', ('cwl_round',)), ('war', ('war_id',))), result_entity_keys=(('players[].player_tag', 'clash_account'), ('season', 'cwl_season'), ('clan_code', 'clan'))),
    "read_cwl_bonus_scope": CapabilityContract((('clan_code', 'clan'),), ('season', 'scope_type', 'cwl_round', 'war_tag'), 'scope_type', (('season', ()), ('round', ('cwl_round',)), ('war', ('war_tag',)))),
    "find_rosters": CapabilityContract((), ()),
    "list_roster_cycles": CapabilityContract((('roster_id', 'roster'),), ('before_id',)),
    "read_roster": CapabilityContract((('roster_id', 'roster'),), ('cycle_id',), result_entity_keys=(('accounts[].player_tag', 'clash_account'), ('accounts[].discord_user_id', 'discord_member'), ('roster_id', 'roster'), ('cycle_id', 'roster_cycle'))),
    "read_regular_war": CapabilityContract((('clan_code', 'clan'),), ('selected',), result_entity_keys=(('members[].player_tag', 'clash_account'), ('clan_code', 'clan'))),
    "read_historical_regular_wars": CapabilityContract((('clan_code', 'clan'),), ('ended_from_ts', 'ended_before_ts', 'before_war_id', 'history_limit'), result_entity_keys=(('war_rows[].source.player_tag', 'clash_account'), ('war_rows[].linked_member_id', 'current_discord_member_link')), time_window=('ended_from_ts', 'ended_before_ts', 'unix_seconds')),
    "read_missing_elder_accounts": CapabilityContract((('clan_codes', 'clan_set'), ('clan_code', 'clan'), ('member_id', 'discord_member')), ()),
    "read_missing_elder_report": CapabilityContract((('report_id', 'missing_elder_report'), ('clan_code', 'clan'), ('member_id', 'discord_member')), ()),
    "find_clan_health_players": CapabilityContract((('clan_code', 'clan'),), ()),
    "get_player_health": CapabilityContract((('player_tag', 'clash_account'),), ('days',)),
    "list_clan_health_reports": CapabilityContract((('clan_code', 'clan'),), ('before_run_id',)),
    "get_clan_health": CapabilityContract((('clan_code', 'clan'),), ()),
    "read_clan_health_period": CapabilityContract((('clan_code', 'clan'), ('run_id', 'clan_health_run')), ()),
    "read_clan_health_report": CapabilityContract((('report_id', 'clan_health_report'),), ()),
    "compare_clan_health_reports": CapabilityContract((('before_report_id', 'clan_health_report'), ('after_report_id', 'clan_health_report')), ()),
    "read_family_account_movements": CapabilityContract((), ('before_run_id', 'interval_limit')),
    "read_family_account_movement_report": CapabilityContract((('report_id', 'family_movement_report'), ('player_tag', 'clash_account'), ('member_id', 'discord_member'), ('clan_code', 'clan')), (), filter_fields=('view', 'transition')),
    "read_member_achievements": CapabilityContract((('member_id', 'discord_member'),), (), filter_fields=('status',)),
    "read_member_achievement_report": CapabilityContract((('report_id', 'achievement_progress_report'),), (), filter_fields=('status',)),
    "read_achievement_leaderboard": CapabilityContract((), ()),
    "read_achievement_leaderboard_report": CapabilityContract((('report_id', 'achievement_leaderboard_report'),), ()),
    "read_achievement_economy_rules": CapabilityContract((), ()),
    "read_member_inventory": CapabilityContract((('member_id', 'discord_member'),), ()),
    "read_member_coin_history": CapabilityContract((('member_id', 'discord_member'),), ('after', 'before'), time_window=('after', 'before', 'iso_utc')),
    "read_member_coin_history_report": CapabilityContract((('report_id', 'coin_transaction_report'),), ()),
    "read_raffle": CapabilityContract((), ('month',)),
    "read_raffle_report": CapabilityContract((('report_id', 'raffle_report'),), ()),
    "read_event_schedule": CapabilityContract((), (), filter_fields=('phase', 'event_type'), required_access=frozenset({ACCESS_LEAD})),
    "read_event_schedule_report": CapabilityContract((('report_id', 'event_schedule_report'),), (), filter_fields=('phase', 'event_type'), required_access=frozenset({ACCESS_LEAD})),
    "read_pending_transfer_requests": CapabilityContract((('clan_code', 'destination_clan'),), ()),
    "read_pending_transfer_report": CapabilityContract((('report_id', 'pending_transfer_report'), ('clan_code', 'destination_clan'), ('member_id', 'discord_member')), ()),
    "read_member_lifecycle": CapabilityContract((), (), filter_fields=('platform', 'activity', 'overdue_only')),
    "read_member_lifecycle_report": CapabilityContract((('report_id', 'member_lifecycle_report'),), (), filter_fields=('platform', 'activity', 'overdue_only')),
    "read_active_hibernation": CapabilityContract((), ()),
    "read_active_hibernation_report": CapabilityContract((('report_id', 'hibernation_report'), ('member_id', 'discord_member')), ()),
    "read_accessible_support_tickets": CapabilityContract((('channel_id', 'support_ticket_channel'),), (), source_scope='channel_status', channel_fields=('channel_id',), result_channel_lists=(('tickets', 'channel_id'),), result_sources_within_query=True),
    "read_support_ticket_report": CapabilityContract((('report_id', 'support_ticket_report'), ('channel_id', 'support_ticket_channel'), ('owner_member_id', 'discord_member')), (), source_scope='retained_channel_evidence', channel_fields=('channel_id',), result_channel_lists=(('tickets', 'channel_id'),), result_sources_within_query=True, filter_fields=('owner_can_send', 'activity_status')),
    "read_active_recruitment_trials": CapabilityContract((('ticket_channel_id', 'recruitment_ticket_channel'),), (), source_scope='channel_status', channel_fields=('ticket_channel_id',), result_channel_lists=(('trials', 'ticket_channel_id'),), result_sources_within_query=True),
    "read_active_recruitment_trial_report": CapabilityContract((('report_id', 'recruitment_trial_report'), ('ticket_channel_id', 'recruitment_ticket_channel'), ('applicant_member_id', 'discord_member')), (), source_scope='retained_channel_evidence', channel_fields=('ticket_channel_id',), result_channel_lists=(('trials', 'ticket_channel_id'),), result_sources_within_query=True, filter_fields=('timing_status',)),
    "read_accessible_examination_cases": CapabilityContract((('ticket_channel_id', 'examination_ticket_channel'),), (), source_scope='channel_status', channel_fields=('ticket_channel_id',), result_channel_lists=(('cases', 'ticket_channel_id'),), result_sources_within_query=True),
    "read_examination_case_report": CapabilityContract((('report_id', 'examination_case_report'), ('ticket_channel_id', 'examination_ticket_channel'), ('applicant_member_id', 'discord_member')), (), source_scope='retained_channel_evidence', channel_fields=('ticket_channel_id',), result_channel_lists=(('cases', 'ticket_channel_id'),), result_sources_within_query=True, filter_fields=('case_type', 'workflow_status', 'response_status')),
    "read_active_leadership_records": CapabilityContract((('member_id', 'discord_member'),), (), required_access=frozenset({ACCESS_LEAD_PLUS})),
    "read_leadership_record_report": CapabilityContract((('report_id', 'leadership_record_report'), ('member_id', 'discord_member')), (), required_access=frozenset({ACCESS_LEAD_PLUS}), filter_fields=('category_key', 'incident_type_key', 'search')),
    "cancel_discord_research_job": CapabilityContract((('job_id', 'discord_research_job'),), (), source_scope='conversation_control'),
    "list_discord_research_jobs": CapabilityContract((), ()),
    "list_regular_war_status": CapabilityContract((), ()),
    "read_regular_war_report": CapabilityContract((('report_id', 'regular_war_report'),), (), result_entity_keys=(('members[].player_tag', 'clash_account'), ('clan_code', 'clan'))),
    "read_historical_regular_war_report": CapabilityContract((('report_id', 'historical_war_report'), ('player_tag', 'clash_account'), ('member_id', 'discord_member')), (), filter_fields=('view',), result_entity_keys=(('war_rows[].source.player_tag', 'clash_account'), ('war_rows[].linked_member_id', 'current_discord_member_link'))),
    "read_roster_report": CapabilityContract((('report_id', 'roster_report'),), (), result_entity_keys=(('accounts[].player_tag', 'clash_account'), ('accounts[].discord_user_id', 'discord_member'), ('roster_id', 'roster'), ('cycle_id', 'roster_cycle'))),
    "compare_roster_reports": CapabilityContract((('before_report_id', 'roster_report'), ('after_report_id', 'roster_report')), ()),
    "read_cwl_performance_report": CapabilityContract((('report_id', 'cwl_performance_report'), ('clan_code', 'clan'), ('player_tag', 'clash_account')), ('season',), result_entity_keys=(('players[].player_tag', 'clash_account'), ('players[].season', 'cwl_season'), ('players[].clan_code', 'clan'))),
    "read_cwl_threads": CapabilityContract((), ()),
    "read_cwl_ass_scope_report": CapabilityContract((('report_id', 'cwl_ass_report'),), (), result_entity_keys=(('players[].player_tag', 'clash_account'), ('season', 'cwl_season'), ('clan_code', 'clan'))),
    "read_cwl_bonus_scope_report": CapabilityContract((('report_id', 'cwl_bonus_report'),), ()),
    "read_csv_import": CapabilityContract((('report_id', 'csv_import'),), (), source_scope='retained_attachment', result_channel_fields=('source_channel_id',)),
    "read_xlsx_import": CapabilityContract((('report_id', 'xlsx_import'), ('sheet_name', 'workbook_sheet')), (), source_scope='retained_attachment', result_channel_fields=('source_channel_id',)),
    "read_text_import": CapabilityContract((('report_id', 'text_import'),), (), source_scope='retained_attachment', result_channel_fields=('source_channel_id',)),
    "read_approved_knowledge_report": CapabilityContract((('report_id', 'approved_knowledge_report'), ('section_id', 'approved_knowledge_section')), ()),
    "read_agent_action_log": CapabilityContract((), (), result_entity_keys=(('actions[].log_id', 'agent_action_log'),)),
    "undo_agent_action": CapabilityContract((('log_id', 'agent_action_log'),), ()),
    "add_discord_roles": CapabilityContract((('role_id', 'discord_role'), ('member_ids', 'discord_member_set')), (), source_scope='request_context'),
    "remove_discord_roles": CapabilityContract((('role_id', 'discord_role'), ('member_ids', 'discord_member_set')), (), source_scope='request_context'),
    "find_agent_files": CapabilityContract((), (), source_scope='request_context', filter_fields=('offset', 'limit'), result_entity_keys=(('files[].message_id', 'agent_file_message'),)),
    "post_discord_message": CapabilityContract((('channel_id', 'discord_channel'), ('ping_role_ids', 'discord_role_set'), ('file_message_id', 'agent_file_message')), (), source_scope='request_context', filter_fields=('text', 'ping_everyone', 'file_name')),
    "edit_agent_message": CapabilityContract((('channel_id', 'discord_channel'), ('message_id', 'discord_message'), ('ping_role_ids', 'discord_role_set')), (), source_scope='request_context', filter_fields=('text', 'ping_everyone')),
    "delete_agent_message": CapabilityContract((('channel_id', 'discord_channel'), ('message_id', 'discord_message')), (), source_scope='request_context'),
    "create_discord_thread": CapabilityContract((('parent_channel_id', 'parent_discord_channel'),), (), source_scope='request_context', filter_fields=('name', 'private', 'initial_message')),
    "update_discord_thread": CapabilityContract((('thread_id', 'discord_channel'),), (), source_scope='request_context', filter_fields=('operation', 'name')),
    "change_discord_thread_members": CapabilityContract((('thread_id', 'discord_channel'), ('member_ids', 'discord_member_set')), (), source_scope='request_context', filter_fields=('operation',)),
    "change_bot_reaction": CapabilityContract((('channel_id', 'discord_channel'), ('message_id', 'discord_message')), (), source_scope='request_context', filter_fields=('operation', 'emoji')),
    "change_discord_pin": CapabilityContract((('channel_id', 'discord_channel'), ('message_id', 'discord_message')), (), source_scope='request_context', filter_fields=('operation',)),
    "change_discord_nickname": CapabilityContract((('member_id', 'discord_member'),), (), source_scope='request_context', filter_fields=('nickname',)),
    "open_roster": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "close_roster": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "show_roster_controls": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "hide_roster_controls": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "clear_roster_signups": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "set_roster_layout": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context', filter_fields=('show_townhall', 'show_discord', 'show_clan', 'player_width', 'discord_width')),
    "refresh_roster": CapabilityContract((('roster_id', 'roster'),), (), source_scope='request_context'),
    "signup_roster_accounts": CapabilityContract((('roster_id', 'roster'), ('member_id', 'discord_member'), ('accounts', 'clash_account_set')), (), source_scope='request_context'),
    "remove_roster_accounts": CapabilityContract((('roster_id', 'roster'), ('member_id', 'discord_member'), ('accounts', 'clash_account_set')), (), source_scope='request_context'),
    "bulk_add_roster_accounts": CapabilityContract((('roster_id', 'roster'), ('player_tags', 'clash_account_set')), (), source_scope='request_context'),
    "clear_transfer_queue": CapabilityContract((('clan_code', 'clan'),), (), source_scope='request_context'),
    "set_event_enabled": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context', filter_fields=('enabled',)),
    "set_event_category": CapabilityContract((('event', 'event_tracker'), ('category_id', 'discord_channel')), (), source_scope='request_context'),
    "move_event": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context', filter_fields=('position', 'edge')),
    "restore_event_defaults": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context'),
    "delete_event": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context'),
    "review_cwl_bonus": CapabilityContract((('clan_code', 'clan'),), (), source_scope='request_context', filter_fields=('mode', 'month_key', 'decision', 'source', 'source_text')),
    "set_cwl_bonus_scoring": CapabilityContract((('clan_code', 'clan'), ('source_clan', 'clan')), (), source_scope='request_context', filter_fields=('operation', 'attacker_th', 'defender_th', 'score', 'uphit_bonus_per_level', 'downhit_penalty_per_level', 'downhit_severe_after', 'downhit_severe_base', 'downhit_severe_multiplier')),
    "create_event_tracker": CapabilityContract((), (), source_scope='request_context', filter_fields=('name', 'start', 'end', 'timezone', 'grace_hours')),
    "edit_event_tracker": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context', filter_fields=('name', 'start', 'end', 'timezone', 'grace_hours')),
    "edit_preset_event": CapabilityContract((('event', 'event_tracker'),), (), source_scope='request_context', filter_fields=('name', 'grace_hours')),
    "manage_role_connection": CapabilityContract((('connection_id', 'role_connection'), ('target_role_id', 'discord_role'), ('channel_id', 'discord_channel')), (), source_scope='request_context', filter_fields=('operation', 'all', 'any')),
    "remove_role_connection": CapabilityContract((('connection_id', 'role_connection'), ('channel_id', 'discord_channel')), (), source_scope='request_context'),
    "apply_role_connections": CapabilityContract((), (), source_scope='request_context'),
    "buy_raffle_ticket": CapabilityContract((), (), source_scope='request_context'),
    "publish_lead_news": CapabilityContract((('message_id', 'discord_message'),), (), source_scope='request_context'),
    "refresh_missing_elder_board": CapabilityContract((('clan_code', 'clan'),), (), source_scope='request_context'),
    "refresh_cwl_prep_board": CapabilityContract((('clan_code', 'clan'),), (), source_scope='request_context'),
    "set_clan_health_settings": CapabilityContract((('clan_code', 'clan'),), (), source_scope='request_context', filter_fields=('block', 'values')),
    "set_examiner_profile": CapabilityContract((), (), source_scope='request_context', filter_fields=('th_levels', 'status', 'timezone', 'availability')),
    "leave_examiner_roster": CapabilityContract((), (), source_scope='request_context'),
    "reopen_support_ticket": CapabilityContract((('channel_id', 'support_ticket_channel'),), (), source_scope='request_context', channel_fields=('channel_id',)),
    "reopen_reactivation_ticket": CapabilityContract((('channel_id', 'support_ticket_channel'),), (), source_scope='request_context', channel_fields=('channel_id',)),
    "close_reactivation_ticket": CapabilityContract((('channel_id', 'support_ticket_channel'),), (), source_scope='request_context', channel_fields=('channel_id',)),
    "end_recruitment_trial": CapabilityContract((('ticket_channel_id', 'recruitment_ticket_channel'), ('applicant_id', 'discord_member')), (), source_scope='request_context', channel_fields=('ticket_channel_id',)),
    "change_promotion_route": CapabilityContract((('ticket_channel_id', 'examination_ticket_channel'),), (), source_scope='request_context', channel_fields=('ticket_channel_id',), filter_fields=('from_clan', 'to_clan')),
    "remember_task_instruction": CapabilityContract((('replaces_id', 'task_instruction'),), (), source_scope='request_context'),
    "retire_task_instruction": CapabilityContract((('instruction_id', 'task_instruction'),), (), source_scope='request_context'),
    "prepare_spreadsheet": CapabilityContract((), (), source_scope='request_context'),
    "prepare_report_spreadsheet": CapabilityContract((), (), source_scope='request_context'),
}


_LATEST_FIELDS = {
    "list_roster_cycles": ("before_id",),
    "find_discord_threads": ("cursor",),
    "get_player_health": ("days",),
    "list_clan_health_reports": ("before_run_id",),
    "read_family_account_movements": ("before_run_id", "interval_limit"),
    "read_regular_war": ("selected",),
    "read_historical_regular_wars": ("before_war_id", "history_limit"),
    "list_roster_cycles": ("before_id",),
    "read_cwl_performance": ("history_limit",),
}
_BOUNDED_FIELDS = {
    "search_discord_messages": ("cursor",),
    "read_discord_channel_history": ("cursor",),
    "read_historical_regular_wars": ("before_war_id",),
}
_PERIOD_RESULTS = {
    "list_roster_cycles": (("cycles", 0, "id"),),
    "list_clan_health_reports": (("reports", 0, "run_id"),),
    "list_cwl_ass_seasons": (("seasons", 0), ("latest_seven_war_season",)),
}
_VALUE_PATTERNS = {
    "read_raffle": (("month", r"20\d{2}-(0[1-9]|1[0-2])"),),
    "read_cwl_performance": (("season", r"20\d{2}-(0[1-9]|1[0-2])"),),
    "read_cwl_performance_report": (("season", r"20\d{2}-(0[1-9]|1[0-2])"),),
    "read_cwl_ass_scope": (("season", r"20\d{2}-(0[1-9]|1[0-2])"),),
    "read_cwl_bonus_scope": (("season", r"20\d{2}-(0[1-9]|1[0-2])"),),
}
_RETAINED_KINDS = frozenset({
    "discord_research_job", "discord_research_job_set", "discord_research_report",
    "role_account_report", "achievement_progress_report", "achievement_leaderboard_report",
    "coin_transaction_report", "raffle_report", "event_schedule_report", "missing_elder_report",
    "clan_health_report", "family_movement_report", "regular_war_report", "historical_war_report",
    "roster_report", "cwl_performance_report", "cwl_ass_report", "cwl_bonus_report",
    "pending_transfer_report", "member_lifecycle_report", "hibernation_report",
    "support_ticket_report", "recruitment_trial_report", "examination_case_report",
    "leadership_record_report", "csv_import", "xlsx_import", "text_import",
    "approved_knowledge_report",
})
CONTRACTS = {
    name: replace(
        contract, latest_fields=_LATEST_FIELDS.get(name, ()),
        bounded_fields=_BOUNDED_FIELDS.get(name, ()),
        period_results=_PERIOD_RESULTS.get(name, ()),
        value_patterns=_VALUE_PATTERNS.get(name, ()),
        retained_fields=tuple(field for field, kind in contract.entity_fields if kind in _RETAINED_KINDS),
    )
    for name, contract in CONTRACTS.items()
}
SAVED_REPORT_CONTRACTS = {
    name: contract for name, contract in CONTRACTS.items()
    if (name.startswith("read_") and (name.endswith("_report") or name.endswith("_import")))
    or (name.startswith("compare_") and name.endswith("_reports"))
}
CONTRACTS = {name: contract for name, contract in CONTRACTS.items()
             if name not in SAVED_REPORT_CONTRACTS}
_SAVED_REPORT_FIELDS = tuple(sorted({
    field for name, contract in SAVED_REPORT_CONTRACTS.items()
    if name.startswith("read_")
    for field in (
        *(field for field, _ in contract.entity_fields),
        *contract.time_fields, *contract.filter_fields, *contract.channel_fields,
    )
    if field != "report_id"
}))
CONTRACTS["read_saved_report"] = CapabilityContract(
    (("report_id", "saved_report"),), (), filter_fields=_SAVED_REPORT_FIELDS,
    retained_fields=("report_id",),
)
CONTRACTS["compare_saved_reports"] = CapabilityContract(
    (("before_report_id", "saved_report"), ("after_report_id", "saved_report")),
    (), retained_fields=("before_report_id", "after_report_id"),
)


def validate_contract_catalogue(registry: Mapping[str, RegisteredAgentTool]) -> None:
    """Catch descriptor drift when a feature changes an exposed query schema."""
    missing_tools = set(CONTRACTS) - set(registry)
    if missing_tools:
        raise ValueError(f"Capability contracts name missing tools: {sorted(missing_tools)}")
    missing_contracts = set(registry) - set(CONTRACTS)
    if missing_contracts:
        raise ValueError(f"Agent tools lack capability contracts: {sorted(missing_contracts)}")
    for name, contract in CONTRACTS.items():
        if not isinstance(contract.required_access, frozenset) or not contract.required_access <= KNOWN_ACCESS_REQUIREMENTS:
            raise ValueError(f"Invalid access requirements in capability contract: {name}")

        def check_unique(labels: tuple[str, ...], category: str) -> None:
            if any(not isinstance(label, str) or not label.strip() for label in labels) or len(
                labels
            ) != len(set(labels)):
                raise ValueError(f"Invalid {category} in capability contract: {name}")

        for category, labels in (
            ("entity fields", tuple(field for field, _ in contract.entity_fields)),
            ("time fields", contract.time_fields),
            ("latest fields", contract.latest_fields),
            ("bounded fields", contract.bounded_fields),
            ("channel fields", contract.channel_fields),
            ("result channel fields", contract.result_channel_fields),
            ("filter fields", contract.filter_fields),
            ("result entity keys", tuple(field for field, _ in contract.result_entity_keys)),
            ("result channel lists", tuple(field for field, _ in contract.result_channel_lists)),
            ("scope variants", tuple(variant for variant, _ in contract.scope_variants)),
        ):
            check_unique(labels, category)
        for category, pairs in (
            ("entity kinds", contract.entity_fields),
            ("result entity kinds", contract.result_entity_keys),
            ("result channel list keys", contract.result_channel_lists),
        ):
            if any(not isinstance(value, str) or not value.strip() for _, value in pairs):
                raise ValueError(f"Invalid {category} in capability contract: {name}")
        if bool(contract.scope_field) != bool(contract.scope_variants):
            raise ValueError(f"Incomplete scope variants in capability contract: {name}")
        if contract.scope_field is not None and not contract.scope_field.strip():
            raise ValueError(f"Invalid scope field in capability contract: {name}")
        for _, selectors in contract.scope_variants:
            check_unique(selectors, "scope selectors")

        fields = set(registry[name].definition.parameters.get("properties", {}))
        described = (
            {field for field, _ in contract.entity_fields}
            | set(contract.time_fields)
            | ({contract.scope_field} if contract.scope_field else set())
            | {field for _, variant in contract.scope_variants for field in variant}
            | set(contract.channel_fields)
            | set(contract.filter_fields)
        )
        if not described <= fields:
            raise ValueError(f"Capability contract and query fields differ: {name}")
        if not set(contract.latest_fields) <= set(contract.time_fields):
            raise ValueError(f"Latest selectors differ from time fields: {name}")
        if not set(contract.retained_fields) <= {field for field, _ in contract.entity_fields}:
            raise ValueError(f"Retained selectors differ from entity fields: {name}")
        if not {field for field, _ in contract.value_patterns} <= fields:
            raise ValueError(f"Value formats differ from query fields: {name}")
        for _, pattern in contract.value_patterns:
            re.compile(pattern)
        if any(not path or any(not (isinstance(part, str) and part or type(part) is int and part >= 0) for part in path)
               for path in contract.period_results):
            raise ValueError(f"Invalid period result path: {name}")
        if not set(contract.bounded_fields) <= set(contract.time_fields) or contract.bounded_fields and contract.time_window is None:
            raise ValueError(f"Bounded selectors differ from time fields: {name}")
        if not fields <= described | MECHANICAL_FIELDS:
            raise ValueError(f"Agent query fields lack capability classification: {name}")
        if contract.scope_field is not None:
            scope_schema = registry[name].definition.parameters["properties"][contract.scope_field]
            if set(scope_schema.get("enum", ())) != {
                variant for variant, _ in contract.scope_variants
            }:
                raise ValueError(f"Scope variants differ from query schema: {name}")
        if contract.time_window is not None:
            window = contract.time_window
            if (
                not isinstance(window, tuple) or len(window) != 3
                or window[0] == window[1]
                or window[2] not in {"iso_utc", "unix_seconds"}
                or not set(window[:2]) <= set(contract.time_fields)
            ):
                raise ValueError(f"Invalid time window in capability contract: {name}")
            expected_type = "string" if window[2] == "iso_utc" else "integer"
            properties = registry[name].definition.parameters["properties"]
            if any(properties[field].get("type") != expected_type for field in window[:2]):
                raise ValueError(f"Time window differs from query schema: {name}")
        if contract.source_scope not in {
            "other", "channel_messages", "channel_status", "channel_locator",
            "request_attachment",
            "retained_channel_evidence", "retained_attachment",
            "conversation_control", "request_context",
        }:
            raise ValueError(f"Unknown source-scope kind: {name}")


def bound_time_window(
    contract: CapabilityContract, arguments: Mapping[str, Any],
) -> tuple[datetime | None, datetime | None]:
    """Decode a capability's exact inclusive-start, exclusive-end UTC window."""
    if contract.time_window is None:
        return None, None
    lower_field, upper_field, encoding = contract.time_window

    def decode(field: str) -> datetime | None:
        if field not in arguments:
            return None
        value = arguments[field]
        if encoding == "iso_utc":
            if not isinstance(value, str):
                raise CapabilityBindError("The selected time boundaries must be ISO dates.")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise CapabilityBindError("The selected time boundaries must be ISO dates.") from error
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        if type(value) is not int or value < 0:
            raise CapabilityBindError("The selected end-time boundaries must be UTC Unix seconds.")
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as error:
            raise CapabilityBindError("The selected end-time boundaries must be UTC Unix seconds.") from error

    lower, upper = decode(lower_field), decode(upper_field)
    if lower is not None and upper is not None and lower >= upper:
        raise CapabilityBindError(
            "The selected time window must be increasing."
            if encoding == "iso_utc" else
            "The selected end-time window must be increasing."
        )
    return lower, upper


def compile_capability_call(
    tool: RegisteredAgentTool, arguments: Mapping[str, Any],
    known_sources: Mapping[str, int] | None = None,
    *, contract: CapabilityContract | None = None,
) -> dict[str, Any]:
    """Return a bounded, inspectable scope; feature code still owns the calculation."""
    contract = contract or CONTRACTS.get(tool.definition.name)
    if contract is None:
        return {"precision": "schema_only", "capability": tool.definition.name}

    entities = []
    for field, kind in contract.entity_fields:
        if field not in arguments:
            continue
        value = arguments[field]
        if kind == "clash_account":
            value = normalize_player_tag(value)
            if value is None:
                raise CapabilityBindError("The account key is not a valid Clash player tag.")
        elif kind == "clash_account_set":
            if not isinstance(value, list):
                raise CapabilityBindError("Account keys must be a list of Clash player tags.")
            normalized = [
                normalize_player_tag(item) if isinstance(item, str) else None
                for item in value
            ]
            if any(item is None for item in normalized):
                raise CapabilityBindError("An account key is not a valid Clash player tag.")
            value = normalized
        entities.append({"kind": kind, "field": field, "key": value})

    temporal_scope = {
        field: arguments[field]
        for field in contract.time_fields if field in arguments
    }
    for field, pattern in contract.value_patterns:
        if field in arguments and (not isinstance(arguments[field], str) or re.fullmatch(pattern, arguments[field]) is None):
            raise CapabilityBindError(f"The selected {field} does not match {pattern}.")
    bound_time_window(contract, arguments)
    if contract.scope_field:
        selected = arguments.get(contract.scope_field)
        variants = dict(contract.scope_variants)
        if selected not in variants:
            raise CapabilityBindError("The selected scope is unavailable for this metric.")
        expected = set(variants[selected])
        selectors = {field for fields in variants.values() for field in fields}
        if any((field in arguments) != (field in expected) for field in selectors):
            raise CapabilityBindError("The selected scope needs its exact round or war key.")
    bound_sources: tuple[int, ...] = ()
    if contract.source_scope in {
        "retained_channel_evidence", "retained_attachment",
    } and known_sources:
        identities = [
            identity for entity in entities
            if entity["kind"] in {
                "discord_research_job", "discord_research_job_set",
                "discord_research_report", "csv_import", "xlsx_import",
                "text_import", "support_ticket_report",
                "recruitment_trial_report", "examination_case_report",
            }
            for identity in (
                entity["key"] if isinstance(entity["key"], list)
                else [entity["key"]]
            )
        ]
        if identities and all(
            type(known_sources.get(identity)) is int
            and known_sources[identity] > 0 for identity in identities
        ):
            bound_sources = tuple(sorted({
                known_sources[identity] for identity in identities
            }))

    return {
        "precision": "typed_v1",
        "capability": tool.definition.name,
        "entities": entities,
        "temporal_scope": temporal_scope,
        "predicates": {
            field: arguments[field] for field in contract.filter_fields
            if field in arguments
        },
        "required_access": tuple(sorted(contract.required_access)),
        "source_scope": contract.source_scope,
        "channel_fields": contract.channel_fields,
        "result_channel_lists": contract.result_channel_lists,
        "result_channel_fields": contract.result_channel_fields,
        "result_sources_within_query": contract.result_sources_within_query,
        "bound_source_channels": bound_sources,
    }


def require_source_provenance(
    capability_scope: Mapping[str, Any], arguments: Mapping[str, Any],
    source_channels: set[int], payload: Mapping[str, Any],
) -> None:
    """A successful channel read must bind every explicitly selected source."""
    if capability_scope.get("source_scope") not in {
        "channel_messages", "channel_status", "retained_channel_evidence",
        "request_attachment", "retained_attachment",
    }:
        return
    requested: set[int] = set()
    for field in capability_scope.get("channel_fields", ()):
        value = arguments.get(field)
        if type(value) is int:
            requested.add(value)
        elif isinstance(value, list):
            requested.update(item for item in value if type(item) is int)
    if not requested <= source_channels:
        raise CapabilityBindError("The lookup omitted its requested channel provenance.")
    returned: set[int] = set()
    for field in capability_scope.get("result_channel_fields", ()):
        value = payload.get(field)
        if type(value) is not int:
            raise CapabilityBindError("The lookup returned an unbound source identity.")
        returned.add(value)
    for collection, field in capability_scope.get("result_channel_lists", ()):
        rows = payload.get(collection)
        if not isinstance(rows, list):
            raise CapabilityBindError("The lookup omitted its source-bearing result rows.")
        for row in rows:
            value = row.get(field) if isinstance(row, Mapping) else None
            if type(value) is not int:
                raise CapabilityBindError("The lookup returned an unbound source identity.")
            returned.add(value)
    bound_sources = set(capability_scope.get("bound_source_channels", ()))
    if not returned <= source_channels or (
        bound_sources and not returned <= bound_sources
    ) or (
        requested and capability_scope.get("result_sources_within_query")
        and not returned <= requested
    ):
        raise CapabilityBindError("The lookup returned evidence outside its bound source scope.")


__all__ = [
    "CapabilityContract", "CONTRACTS", "CapabilityBindError",
    "bound_time_window", "compile_capability_call", "require_source_provenance",
    "validate_contract_catalogue",
]
