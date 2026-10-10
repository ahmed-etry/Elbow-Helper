"""Fields available for references before capabilities execute."""

from elbow_helper.features.achievements.queries import AchievementProgressRow
from elbow_helper.features.clan_reporting.queries import MissingElderRow
from elbow_helper.features.clan_transfers.queries import PendingTransferRequest
from elbow_helper.features.cwl.queries import (
    CwlAssWarsSnapshot, CwlBonusWarsSnapshot, CwlClanSeasonSummary, CwlPerformanceRow,
    CwlThreadRegistration,
)
from elbow_helper.features.event_stats.queries import EventScheduleRow
from elbow_helper.features.examination.queries import ExaminationCaseStatus
from elbow_helper.features.hibernation.queries import ActiveHibernationRecord
from elbow_helper.features.help.discovery import ParameterInfo
from elbow_helper.features.member_lifecycle.queries import MemberLifecycleRow, OverdueApplicant
from elbow_helper.features.recruitment.queries import ActiveRecruitmentTrial
from elbow_helper.features.role_connections.queries import RoleConnectionCondition
from elbow_helper.features.support_tickets.queries import SupportTicketMetadata

from ..conversation.instructions import TaskInstruction
from ..datasets.clash import (
    CLAN_FIELDS, CLAN_MEMBER_FIELDS, CWL_CLAN_FIELDS, CWL_GROUP_FIELDS, CWL_MEMBER_FIELDS,
    HERO_FIELDS, LEAGUE_FIELDS, PLAYER_CLAN_FIELDS, PLAYER_FIELDS, WAR_CLAN_FIELDS,
    WAR_LOG_FIELDS, WAR_SIDE_FIELDS,
)
from ..plan.results import LIMIT_FIELDS
from .result_fields import format_returns, names, result_fields


MESSAGE = names("message_id,channel_id,channel,author_id,author,timestamp,content,source")
CONTEXT_MESSAGE = names("message_id,author_id,author,timestamp,content,source")
CLASH_SUMMARIES = {
    "player": {**dict.fromkeys(PLAYER_FIELDS, ""), "clan": dict.fromkeys(PLAYER_CLAN_FIELDS, ""),
               "league": dict.fromkeys(LEAGUE_FIELDS, ""),
               "heroes": [dict.fromkeys(HERO_FIELDS, "")]},
    "clan": {**dict.fromkeys(CLAN_FIELDS, ""), "warLeague": dict.fromkeys(LEAGUE_FIELDS, ""),
             "capitalLeague": dict.fromkeys(LEAGUE_FIELDS, ""),
             "memberList": [dict.fromkeys(CLAN_MEMBER_FIELDS, "")]},
    "cwl_group": {**dict.fromkeys(CWL_GROUP_FIELDS, ""), "clans": [{
        **dict.fromkeys(CWL_CLAN_FIELDS, ""), "members": [dict.fromkeys(CWL_MEMBER_FIELDS, "")],
    }], "rounds": [names("warTags")]},
    "war_log": {"items": [{**dict.fromkeys(WAR_LOG_FIELDS, ""),
                           "clan": dict.fromkeys(WAR_CLAN_FIELDS, ""),
                           "opponent": dict.fromkeys(WAR_SIDE_FIELDS, "")}]},
}
HISTORY_COVERAGE = names(
    "requested_after,requested_before,window_after_message_id,snapshot_before_message_id,"
    "page_before_message_id,scanned_messages_in_window,returned_messages,next_cursor,"
    "reached_requested_start,covers_currently_available_messages_only"
)
SEARCH_COVERAGE = names(
    "offset,requested_page_size,returned_indexed_matches,returned_accessible_matches,"
    "total_results_estimate,next_offset,next_cursor,snapshot_before_message_id,"
    "deep_historical_indexing,offset_limit_reached,reached_current_indexed_end,"
    "total_may_change_while_messages_are_created_or_deleted"
)
JOB = names(
    "job_id,status,kind,source_channel_id,query,author_id,after,before,page_size,"
    "pages_completed,retained_message_count,total_results_estimate,continuable,"
    "cancellation_requested,error_class,created_at,updated_at,expires_at,version,coverage"
)
JOB["coverage"] = {
    key + "?": value for key, value in {**HISTORY_COVERAGE, **SEARCH_COVERAGE, **names(
        "retained_storage_limit_reached,retained_message_limit_reached,repeated_cursor_detected"
    )}.items()
}
RESEARCH_REPORT = {
    **names(
        "report_id,kind,source_job_id,research_topic_fingerprint,source_channel_id,research_kind,"
        "query,author_id,after,before,observed_at,job_status,coverage_complete,pages_completed,"
        "retained_message_count,total_results_estimate,message_ids_sha256,messages_sha256,"
        "oldest_message_at,newest_message_at,author_counts,coverage,limitations,next_offset,"
        "complete_retained_snapshot,interpretation"
    ), "messages": [MESSAGE], "author_counts": "{*}", "coverage": JOB["coverage"],
}
CSV_REPORT = {
    **names(
        "report_id,kind,filename,imported_at,rows,columns,issues,source_message_id,"
        "source_channel_id,"
        "attachment_id,content_sha256,byte_size,encoding,delimiter,total_rows,next_offset,"
        "complete_import"
    ), "data": ["[]"],
}
XLSX_REPORT = {
    **names(
        "report_id,kind,filename,imported_at,source_message_id,source_channel_id,attachment_id,"
        "content_sha256,byte_size,uncompressed_size,sheet_name,columns,issues,total_rows,"
        "next_offset,"
        "complete_import"
    ), "sheets": [names("name,rows,columns,issues")], "data": ["[]"],
}
TEXT_REPORT = names(
    "report_id,kind,filename,imported_at,document_format,characters,lines,content_sha256,"
    "interpretation,source_message_id,source_channel_id,attachment_id,byte_size,encoding,"
    "offset,text,next_offset,complete_import"
)
REPORT_FIELDS = {
    "discord_research": RESEARCH_REPORT,
    "achievement_progress": {
        **names("report_id,kind,observed_at,member_id,member_name,completed_count,"
                "in_progress_count,"
                "total_count,status_filter,matching_rows,next_offset,complete_snapshot"),
        "achievements": [result_fields(AchievementProgressRow)],
    },
    "event_schedule": {
        **names("report_id,kind,observed_at,event_count,enabled_count,phase_counts,"
                "counter_coverage_counts,matching_rows,next_offset,complete_snapshot"),
        "filters": names("phase,event_type"), "events": [result_fields(EventScheduleRow)],
        "phase_counts": "{*}", "counter_coverage_counts": "{*}",
    },
    "missing_elder": {
        **names("report_id,kind,observed_at,selected_clan_codes,omitted_inaccessible_clan_codes,"
                "missing_account_count,missing_account_counts_by_clan,skipped_invalid_row_count,"
                "complete_for_accessible_selected_clans,calculation_scope,matching_rows,"
                "next_offset,complete_snapshot"),
        "filters": names("clan_code,member_id"), "accounts": [result_fields(MissingElderRow)],
        "missing_account_counts_by_clan": "{*}",
    },
    "cwl_performance": {
        **names("report_id,kind,observed_at,history_limit,seasons,row_count,matching_rows,"
                "matching_accounts,attacks,attacks_expected,next_offset,complete_snapshot"),
        "filters": names("season,clan_code,player_tag"),
        "clan_seasons": [result_fields(CwlClanSeasonSummary)],
        "players": [result_fields(CwlPerformanceRow)],
    },
    "pending_transfer_requests": {
        **names("report_id,kind,observed_at,request_ttl_hours,registered_queue_count,"
                "accessible_queue_count,omitted_inaccessible_count,pending_request_count,"
                "expired_stored_count,complete_accessible_queue_snapshot,interpretation,"
                "matched_count,next_offset"),
        "queue_summaries": [names("clan_code,thread_id,url,pending_count,stored_request_count,"
                                  "expired_stored_count,oldest_created_at,next_expiry_at")],
        "requests": [{**names("clan_code,thread_id"), **result_fields(PendingTransferRequest)}],
    },
    "member_lifecycle": {
        **names("report_id,kind,observed_at,tracked_current_member_count,"
                "current_guild_member_count,"
                "untracked_current_member_count,overdue_applicant_count,activity_observation_count,"
                "last_weekly_report_at,last_applicant_scan_at,matching_rows,next_offset,"
                "complete_snapshot"),
        "coverage": names("stored_member_entry_count,skipped_invalid_member_entries,"
                          "skipped_invalid_platform_counts,skipped_invalid_overdue_entries"),
        "platform_counts": [names("platform,count")],
        "overdue_applicants": [result_fields(OverdueApplicant)],
        "filters": names("platform,activity,overdue_only"),
        "members": [result_fields(MemberLifecycleRow)],
    },
    "active_hibernation": {
        **names("report_id,kind,observed_at,source_channel_id,active_record_count,"
                "missing_start_time_count,skipped_invalid_member_entries,ignored_metadata_entries,"
                "complete_valid_record_snapshot,record_order,included_fields,"
                "excluded_sensitive_fields,"
                "interpretation,matched_count,next_offset"),
        "records": [result_fields(ActiveHibernationRecord)],
    },
    "support_ticket_inventory": {
        **names("report_id,kind,observed_at,registered_ticket_count,accessible_ticket_count,"
                "omitted_inaccessible_count,owner_status_counts,owner_send_status_counts,"
                "tickets_without_last_message_id,oldest_known_activity_at,"
                "complete_accessible_metadata_snapshot,message_history_read,included_fields,"
                "excluded_fields,interpretation,matched_count,next_offset"),
        "tickets": [result_fields(SupportTicketMetadata)],
        "owner_status_counts": "{*}", "owner_send_status_counts": "{*}",
    },
    "active_recruitment_trials": {
        **names("report_id,kind,observed_at,accessible_selected_entry_count,"
                "accessible_active_trial_count,"
                "skipped_invalid_accessible_count,timing_status_counts,"
                "complete_valid_accessible_status_snapshot,message_history_read,included_fields,"
                "excluded_fields,interpretation,matched_count,next_offset"),
        "trials": [result_fields(ActiveRecruitmentTrial)], "timing_status_counts": "{*}",
    },
    "examination_case_status": {
        **names("report_id,kind,observed_at,accessible_selected_entry_count,accessible_case_count,"
                "skipped_invalid_accessible_count,case_type_counts,workflow_status_counts,"
                "response_status_counts,complete_valid_accessible_status_snapshot,"
                "message_history_read,"
                "included_fields,excluded_fields,interpretation,matched_count,next_offset"),
        "cases": [result_fields(ExaminationCaseStatus)],
        **dict.fromkeys(
            ("case_type_counts", "workflow_status_counts", "response_status_counts"), "{*}",
        ),
    },
    "csv_import": CSV_REPORT, "xlsx_import": XLSX_REPORT, "text_import": TEXT_REPORT,
}

ASS = result_fields(CwlAssWarsSnapshot)
ASS["players"] = ASS.pop("rows")
ASS["attack_sample"] = [names(
    "war_id,player_tag,player_name,attack_order,defender_tag,defender_townhall,"
    "defender_map_position,stars,destruction,clan_code"
)]
BONUS = result_fields(CwlBonusWarsSnapshot)
BONUS["rows"] = BONUS.pop("summaries")
BONUS["attack_sample"] = BONUS.pop("attacks")
BONUS.update(metric_name="", metric_definition="", ass_distinction=names("is_ass"))

RESULT_FIELDS = {
    "query_bot_data": {"rows": ["{selected SQL columns}"], **names("row_count,truncated")},
    "read_clash": {"items": [{**names("tag,status"), "data?": "{endpoint fields}"}],
                   "observed_at": ""},
    "read_bot_command_help": {
        **names("matching_count?,next_offset?,source,visibility?,path?,summary?,details?,category?,"
                "examples?,notes?"), "options?": [result_fields(ParameterInfo)],
        "commands?": [names("path,summary,category")],
    },
    "find_discord_channels": {"channels": [names("channel_id,name")],
                              **names("matched_count,truncated")},
    "find_discord_members": {
        "query": "", "members": [names("member_id,display_name,username,joined_at,mention,roles")],
    },
    "read_discord_members": {
        **names("total_members,missing_member_ids,offset,limit,next_offset"),
        "members": [{**names("member_id,display_name,username,joined_at,bot"),
                     "roles?": [names("role_id,name")]}],
    },
    "find_discord_roles": {
        **names("matched_count,next_offset"),
        "roles": [{**names("role_id,name,position,managed,permissions,member_count"),
                   "clan_purposes": [names("clan_code,purpose")]}],
    },
    "find_discord_threads": {
        **names("parent_channel_id,parent_channel,state,visibility"),
        "threads": [names("thread_id,name,parent_channel_id,visibility,archived,"
                          "archive_timestamp")],
        "coverage": names("returned_threads,next_cursor,coverage_complete,continuable,"
                          "private_listing_scope,active_threads_may_change_between_pages,"
                          "incomplete_private_scan_does_not_prove_no_matching_thread"),
    },
    "search_discord_messages": {
        **names("query,search_is_exhaustive"), "matches": [MESSAGE], "coverage?": SEARCH_COVERAGE,
        "filters?": names("query?,channel_id?,channel_ids?,author_id?,after?,before?"),
    },
    "read_discord_channel_history": {
        **names("channel_id,channel"), "messages": [MESSAGE], "coverage": HISTORY_COVERAGE,
    },
    "read_message_context": {
        **names("channel_id,channel,requested_message_id"), "messages": [CONTEXT_MESSAGE],
    },
    "read_discord_research_job": {
        **JOB, "matched_count": "", "messages": [MESSAGE], "next_offset": "",
    },
    "retain_discord_research_report": RESEARCH_REPORT,
    "start_discord_research_batch": {**names("job_count,job_ids"), "jobs": [JOB]},
    "read_discord_research_jobs": {
        **names("job_count,all_terminal"), "status_counts": "{*}", "jobs": [JOB],
    },
    "list_discord_research_jobs": {
        **names("returned_jobs,inaccessible_jobs_omitted,candidate_limit_reached,"
                "idle_expiry_extended"),
        "jobs": [names("job_id,status,source_channel_id,requester_id")],
    },
    "find_agent_files": {"files": [names("message_id,file_name,size")], "next_offset": ""},
    "list_supported_attachments": {
        "count": "", "attachments": [names("attachment_id,filename,content_type,size,kind,source,"
                                            "message_id,channel_id")],
    },
    "import_csv_attachment": CSV_REPORT,
    "import_xlsx_attachment": XLSX_REPORT,
    "import_text_attachment": TEXT_REPORT,
    "read_conversation_history": {
        **names("matching_retained_turns,next_offset,history_may_be_incomplete"),
        "results": [names("request_message_id,member_id,created_at,retention_limited,"
                          "content_offset,"
                          "content,next_content_offset")],
    },
    "read_agent_action_log": {
        "next_offset": "", "actions": [names("log_id,action_name,action_label,action_class,"
                                             "target_links,outcome,executed_at")],
    },
    "list_standing_rules": {"rules": [names("id,kind,request,status,next_at")]},
    "read_clan_health_settings": {
        "clan_code": "", "sections": [{"name": "", "fields": [
            names("name,key,description,value,unit")
        ]}],
    },
    "read_member_inventory": names("observed_at,member_id,member_name,balance,has_current_ticket,"
                                   "current_month_key"),
    "read_achievement_economy_rules": {
        **names("observed_at,elder_monthly_salary_coins,achievement_reward_total_coins"),
        "daily_activity": names("qualifying_messages,minimum_characters_per_message,"
                                "member_coins,elder_coins"),
        "tickets": names("cost_coins,limit_per_month"),
        "manual_monthly_caps": names("cwl_coins,encouragement_coins,combined_coins"),
        "achievement_rewards": "{*}",
    },
    "read_cwl_threads": {
        **names("accessible_count,omitted_inaccessible_count"),
        "threads": [{**result_fields(CwlThreadRegistration), "url": ""}],
    },
    "cwl_ass_scores": ASS, "cwl_bonus_scores": BONUS,
    "read_my_cwl_placement": names("text"), "read_my_cwl_channels": names("text"),
    "read_my_examiner_profile": {
        **names("panel_channel_id,registered,profile_complete"),
        "profile": names("th_levels,status,timezone,availability"),
    },
    "read_examiner_roster": {
        **names("panel_channel_id,total,offset"),
        "profiles": [names("id,name,ths,availability,status,timezone,availability_raw,"
                           "has_th_levels,"
                           "availability_valid,profile_complete,updated_at")],
    },
    "read_promotion_review": names("ticket_channel_id,review_channel_id,text"),
    "read_role_connections": {
        **names("observed_at,state_fingerprint,total_entries,valid_rules,malformed_rule_count,"
                "malformed_rule_indexes,cyclic_rule_count,next_offset,complete_valid_rule_snapshot,"
                "all_entries_valid"),
        "evaluated_member": names("member_id,member_name,role_ids"),
        "rules": [{**names("index,connection_id,target_role_id,target_role_name,cyclic,"
                           "member_currently_has_target?,rule_matches_member?"),
                   **{key: [{**result_fields(RoleConnectionCondition), "role_name": ""}]
                      for key in ("all_conditions", "any_conditions")}}],
    },
    "search_approved_knowledge": {"sections": [names("title,body,visibility")]},
    "remember_task_instruction": {
        **names("working_state_version,action_authorized"),
        "instruction": result_fields(TaskInstruction),
    },
    "retire_task_instruction": names("instruction_id,active,working_state_version,"
                                     "action_authorized"),
    "prepare_spreadsheet": names("filename,sheets,rows,attachment_prepared,replaced_previous"),
    "react_to_request": names("reacted"), "find_gif": names("url"),
}
for name in ("start_discord_research_job", "start_discord_history_job",
             "continue_discord_research_job", "cancel_discord_research_job"):
    RESULT_FIELDS[name] = JOB
RESULT_FIELDS["continue_discord_research_job"] = {**JOB, "new_messages": [MESSAGE]}
for name, kind in {
    "read_member_achievements": "achievement_progress", "read_event_schedule": "event_schedule",
    "read_missing_elder_accounts": "missing_elder", "read_cwl_performance": "cwl_performance",
    "read_pending_transfer_requests": "pending_transfer_requests",
    "read_member_lifecycle": "member_lifecycle", "read_active_hibernation": "active_hibernation",
    "read_accessible_support_tickets": "support_ticket_inventory",
    "read_active_recruitment_trials": "active_recruitment_trials",
    "read_accessible_examination_cases": "examination_case_status",
}.items():
    RESULT_FIELDS[name] = REPORT_FIELDS[kind]
for name, field in {
    "read_pending_transfer_requests": "selected_clan_code",
    "read_accessible_support_tickets": "selected_channel_id",
    "read_active_recruitment_trials": "selected_ticket_channel_id",
    "read_accessible_examination_cases": "selected_ticket_channel_id",
}.items():
    RESULT_FIELDS[name] = {**RESULT_FIELDS[name], field: ""}

def _returned_fields(shape):
    # The shared result boundary moves non-record limitations into flags.
    shape = {key: value for key, value in shape.items()
             if key not in LIMIT_FIELDS or isinstance(value, dict)}
    records = {key for key, value in shape.items()
               if isinstance(value, list) and isinstance(value[0], dict)}
    if not records:
        return shape
    # Page totals and echoed filters are not fields of the returned records.
    identities = {
        "report_id", "job_id", "job_ids", "clan_code", "filename", "observed_at", "source",
        "seasons", "resolved_war_ids", "resolved_rounds", "resolved_war_tags", "completed_wars",
        "settings", "metric_name", "metric_definition", "ass_distinction", "state_fingerprint",
        "panel_channel_id", "selected_clan_code", "selected_channel_id",
        "selected_ticket_channel_id",
    }
    return {key: value for key, value in shape.items() if key in records or key in identities}


RESULT_FIELDS = {name: _returned_fields(shape) for name, shape in RESULT_FIELDS.items()}
REPORT_FIELDS = {kind: _returned_fields(shape) for kind, shape in REPORT_FIELDS.items()}
RETURN_HINTS = {name: format_returns(shape) for name, shape in RESULT_FIELDS.items()}


def capability_returns(tool) -> str:
    """Report routers advertise each selected kind rather than an ambiguous union."""
    name = tool.definition.name
    if name == "read_saved_report":
        return ";".join(kind + ":{" + format_returns(REPORT_FIELDS[kind]) + "}"
                        for kind in tool.handler.specs)
    if name in RETURN_HINTS:
        hint = RETURN_HINTS[name]
        if name == "read_clash":
            hint += ";summary data by kind=" + ";".join(
                kind + ":{" + format_returns(shape) + "}" for kind, shape in CLASH_SUMMARIES.items()
            ) + ";full,war,raid data:{*}"
        return hint
    if tool.effect.value == "command":
        return "status"
    raise ValueError(f"Agent capability lacks result fields: {name}")
