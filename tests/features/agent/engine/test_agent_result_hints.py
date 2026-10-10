"""Registry return fields agree with serializers fed only synthetic values."""

from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime, timezone
from io import BytesIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace, UnionType
from typing import Any, TypedDict, Union, get_args, get_origin, get_type_hints
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from features.agent.files.test_agent_spreadsheets import _context
from features import test_cwl_queries as cwl_fixtures

from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.result_fields import (
    format_returns, result_fields,
)
from elbow_helper.features.agent.engine.result_hints import (
    CLASH_SUMMARIES, REPORT_FIELDS, RESULT_FIELDS,
)
from elbow_helper.features.agent.datasets.clash import summary
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.features.agent.plan.executor import execute_plan
from elbow_helper.features.agent.plan.results import model_result
from elbow_helper.features.agent.models import AgentCapabilityEffect
from elbow_helper.features.agent.capabilities.achievements.report import AchievementProgressReport
from elbow_helper.features.agent.capabilities.clan_reporting.report import MissingElderReport
from elbow_helper.features.agent.capabilities.clan_transfers.report import TransferQueueReport
from elbow_helper.features.agent.capabilities.cwl.report import CwlPerformanceReport
from elbow_helper.features.agent.capabilities.events.report import EventScheduleReport
from elbow_helper.features.agent.capabilities.examination.report import ExaminationCaseReport
from elbow_helper.features.agent.capabilities.hibernation.report import HibernationReport
from elbow_helper.features.agent.capabilities.member_lifecycle.report import MemberLifecycleReport
from elbow_helper.features.agent.capabilities.recruitment.report import RecruitmentTrialReport
from elbow_helper.features.agent.capabilities.support_tickets.report import SupportTicketReport
from elbow_helper.features.agent.files.contracts import (
    CsvImportArtifact, TextImportArtifact, XlsxImportArtifact,
)
from elbow_helper.features.agent.research.contracts import DiscordResearchJob
from elbow_helper.features.agent.research.report import DiscordResearchReport
from elbow_helper.features.agent.research.threads import _thread_result
from elbow_helper.features.agent.capabilities.role_connections.reads import _render_rule
from elbow_helper.features.agent.conversation.instructions import TaskInstruction
from elbow_helper.features.cwl.queries import (
    CwlAssWarsSnapshot, CwlBonusWarsSnapshot, CwlThreadRegistration,
)
from elbow_helper.features.help.discovery import ParameterInfo
from elbow_helper.features.role_connections.queries import RoleConnectionRule


REPORT_TYPES = {
    "discord_research": DiscordResearchReport,
    "achievement_progress": AchievementProgressReport,
    "event_schedule": EventScheduleReport,
    "missing_elder": MissingElderReport,
    "cwl_performance": CwlPerformanceReport,
    "pending_transfer_requests": TransferQueueReport,
    "member_lifecycle": MemberLifecycleReport,
    "active_hibernation": HibernationReport,
    "support_ticket_inventory": SupportTicketReport,
    "active_recruitment_trials": RecruitmentTrialReport,
    "examination_case_status": ExaminationCaseReport,
    "csv_import": CsvImportArtifact, "xlsx_import": XlsxImportArtifact,
    "text_import": TextImportArtifact,
}
READ_KINDS = {
    "read_member_achievements": "achievement_progress", "read_event_schedule": "event_schedule",
    "read_missing_elder_accounts": "missing_elder", "read_cwl_performance": "cwl_performance",
    "read_pending_transfer_requests": "pending_transfer_requests",
    "read_member_lifecycle": "member_lifecycle", "read_active_hibernation": "active_hibernation",
    "read_accessible_support_tickets": "support_ticket_inventory",
    "read_active_recruitment_trials": "active_recruitment_trials",
    "read_accessible_examination_cases": "examination_case_status",
    "retain_discord_research_report": "discord_research",
    "import_csv_attachment": "csv_import", "import_xlsx_attachment": "xlsx_import",
    "import_text_attachment": "text_import",
}


class SyntheticNestedRecord(TypedDict):
    name: str
    value: float


class SyntheticResult(TypedDict):
    records: list[SyntheticNestedRecord]
    complete: bool


def synthetic_value(value_type, seed):
    origin, arguments = get_origin(value_type), get_args(value_type)
    if origin in (Union, UnionType):
        return synthetic_value(next(item for item in arguments if item is not type(None)), seed)
    if origin in (tuple, list):
        if origin is tuple and len(arguments) > 1 and arguments[1] is not Ellipsis:
            return tuple(synthetic_value(item, seed) for item in arguments)
        values = [synthetic_value(arguments[0], seed)]
        return tuple(values) if origin is tuple else values
    if origin is dict:
        return {"synthetic": seed}
    if is_dataclass(value_type):
        # Exercise serialization independently of validation and the hint declarations.
        value = object.__new__(value_type)
        hints = get_type_hints(value_type)
        for field in fields(value_type):
            object.__setattr__(value, field.name, synthetic_value(hints[field.name], seed))
        return value
    if value_type is bool:
        return bool(seed % 2)
    if value_type in (int, float):
        return value_type(seed)
    if value_type is Any:
        return seed
    return "2026-01-01T00:00:00+00:00"


def message(seed):
    return {
        "message_id": seed, "channel_id": 100, "channel": "Synthetic channel",
        "author_id": 42, "author": "Synthetic member", "timestamp": "2026-01-01T00:00:00+00:00",
        "content": "Synthetic message", "source": f"synthetic/{seed}",
    }


def report_page(kind, seed):
    report = synthetic_value(REPORT_TYPES[kind], seed)
    if kind == "discord_research":
        object.__setattr__(report, "messages", (message(seed),))
        object.__setattr__(report, "status", "completed")
    if kind == "csv_import":
        object.__setattr__(report, "delimiter", ",")
    return model_result(report.page())


def fixed_results(seed):
    member = {
        "member_id": 42, "display_name": "Synthetic member", "username": "synthetic",
        "joined_at": "2026-01-01T00:00:00+00:00", "mention": "<@42>", "roles": ["Synthetic role"],
    }
    job = synthetic_value(DiscordResearchJob, seed).manifest()
    context = SimpleNamespace(guild=SimpleNamespace(
        get_role=lambda _: SimpleNamespace(name="Synthetic role"),
    ))
    rule = synthetic_value(RoleConnectionRule, seed)
    ass = asdict(synthetic_value(CwlAssWarsSnapshot, seed))
    ass["players"] = list(ass.pop("rows"))
    ass["attack_sample"] = [{
        "war_id": "CWL:synthetic", "player_tag": "#P0", "player_name": "Synthetic",
        "attack_order": 1, "defender_tag": "#P2", "defender_townhall": 18,
        "defender_map_position": 1, "stars": 3, "destruction": 100, "clan_code": "BEH",
    }]
    bonus = asdict(synthetic_value(CwlBonusWarsSnapshot, seed))
    bonus["rows"] = list(bonus.pop("summaries"))
    bonus["attack_sample"] = bonus.pop("attacks")
    bonus.update(metric_name="Synthetic", metric_definition="Synthetic", ass_distinction={
        "is_ass": False,
    })
    return {
        "query_bot_data": {"rows": [{"synthetic_number": seed}], "row_count": 1,
                           "truncated": False},
        "read_clash": {"items": [{"tag": "#P0", "status": "ok", "data": {"name": "Synthetic"}}],
                       "observed_at": "2026-01-01T00:00:00+00:00"},
        "find_discord_channels": {"channels": [{"channel_id": 100, "name": "Synthetic"}],
                                  "matched_count": 1, "truncated": False},
        "find_discord_members": {"query": "synthetic", "members": [member]},
        "read_discord_members": {
            "members": [{key: value for key, value in member.items() if key not in {"mention"}}
                        | {"bot": False, "roles": [{"role_id": 99, "name": "Synthetic"}]}],
            "total_members": 1, "missing_member_ids": [], "offset": 0, "limit": 1,
            "next_offset": None,
        },
        "find_discord_roles": {
            "roles": [{"role_id": 99, "name": "Synthetic", "position": 1, "managed": False,
                       "permissions": [], "member_count": 1,
                       "clan_purposes": [{"clan_code": "BEH", "purpose": "member_role_id"}]}],
            "matched_count": 1, "next_offset": None,
        },
        "find_discord_threads": {
            "parent_channel_id": 100, "parent_channel": "Synthetic", "state": "active",
            "visibility": "public", "threads": [_thread_result(SimpleNamespace(
                id=seed, name="Synthetic", parent_id=100, is_private=lambda: False,
                archived=False, archive_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ))], "coverage": {
                "returned_threads": 1, "next_cursor": None, "coverage_complete": True,
                "continuable": False, "private_listing_scope": None,
                "active_threads_may_change_between_pages": True,
                "incomplete_private_scan_does_not_prove_no_matching_thread": True,
            },
        },
        "search_discord_messages": {
            "query": "Synthetic", "matches": [message(seed)], "search_is_exhaustive": False,
            "filters": {"query": "Synthetic", "channel_id": 100, "channel_ids": [100],
                        "author_id": 42, "after": "2026-01-01", "before": "2026-01-02"},
            "coverage": {
                "offset": 0, "requested_page_size": 1, "returned_indexed_matches": 1,
                "returned_accessible_matches": 1, "total_results_estimate": 1, "next_offset": None,
                "next_cursor": None, "snapshot_before_message_id": 999,
                "deep_historical_indexing": False, "offset_limit_reached": False,
                "reached_current_indexed_end": True,
                "total_may_change_while_messages_are_created_or_deleted": True,
            },
        },
        "read_discord_channel_history": {
            "channel_id": 100, "channel": "Synthetic", "messages": [message(seed)], "coverage": {
                "requested_after": "2026-01-01", "requested_before": "2026-01-02",
                "window_after_message_id": 1, "snapshot_before_message_id": 999,
                "page_before_message_id": 999, "scanned_messages_in_window": 1,
                "returned_messages": 1, "next_cursor": None, "reached_requested_start": True,
                "covers_currently_available_messages_only": True,
            },
        },
        "read_message_context": {
            "channel_id": 100, "channel": "Synthetic", "requested_message_id": seed,
            "messages": [{key: value for key, value in message(seed).items()
                          if key not in {"channel_id", "channel"}}],
        },
        "read_bot_command_help": {
            "matching_count": 1, "next_offset": None, "source": "/help", "visibility": "public",
            "commands": [{"path": "/synthetic", "summary": "Synthetic", "category": "Synthetic"}],
            "path": "/synthetic", "summary": "Synthetic", "details": "Synthetic",
            "category": "Synthetic", "examples": [], "notes": [],
            "options": [asdict(synthetic_value(ParameterInfo, seed))],
        },
        "read_discord_research_job": {
            **job, "matched_count": 1, "messages": [message(seed)], "next_offset": None,
        },
        **{name: job for name in ("start_discord_research_job", "start_discord_history_job",
                                  "cancel_discord_research_job")},
        "continue_discord_research_job": {**job, "new_messages": [message(seed)]},
        "start_discord_research_batch": {"job_count": 1, "job_ids": [job["job_id"]], "jobs": [job]},
        "read_discord_research_jobs": {"job_count": 1, "status_counts": {"completed": 1},
                                      "all_terminal": True, "jobs": [job]},
        "list_discord_research_jobs": {
            "jobs": [{"job_id": "synthetic", "status": "completed", "source_channel_id": 100,
                      "requester_id": 42}], "returned_jobs": 1, "inaccessible_jobs_omitted": False,
            "candidate_limit_reached": False, "idle_expiry_extended": False,
        },
        "find_agent_files": {"files": [{"message_id": 1, "file_name": "synthetic.xlsx", "size": 7}],
                             "next_offset": None},
        "list_supported_attachments": {
            "count": 1, "attachments": [{"attachment_id": 1, "filename": "synthetic.csv",
                                          "content_type": "text/csv", "size": 7, "kind": "csv",
                                          "source": "request", "message_id": 2, "channel_id": 100}],
        },
        "read_conversation_history": {
            "results": [{"request_message_id": 1, "member_id": 42,
                         "created_at": "2026-01-01T00:00:00+00:00", "retention_limited": False,
                         "content_offset": 0, "content": "Synthetic", "next_content_offset": None}],
            "matching_retained_turns": 1, "next_offset": None, "history_may_be_incomplete": False,
        },
        "read_agent_action_log": {
            "actions": [{"log_id": "synthetic", "action_name": "synthetic", "action_label": "Set",
                         "action_class": "change", "target_links": [], "outcome": "complete",
                         "executed_at": "2026-01-01T00:00:00+00:00"}], "next_offset": None,
        },
        "list_standing_rules": {"rules": [{"id": 1, "kind": "reminder", "request": "Synthetic",
                                           "status": "active", "next_at": seed}]},
        "read_clan_health_settings": {"clan_code": "BEH", "sections": [{"name": "Synthetic",
            "fields": [{"name": "Synthetic", "key": "synthetic", "description": "Synthetic",
                        "value": seed, "unit": "days"}]}]},
        "read_member_inventory": {"observed_at": "2026-01-01T00:00:00+00:00", "member_id": 42,
                                  "member_name": "Synthetic", "balance": seed,
                                  "has_current_ticket": False, "current_month_key": "2026-01"},
        "read_achievement_economy_rules": {
            "observed_at": "2026-01-01T00:00:00+00:00",
            "daily_activity": {"qualifying_messages": 1, "minimum_characters_per_message": 1,
                               "member_coins": 1, "elder_coins": 1},
            "elder_monthly_salary_coins": 1, "tickets": {"cost_coins": 1, "limit_per_month": 1},
            "manual_monthly_caps": {"cwl_coins": 1, "encouragement_coins": 1, "combined_coins": 2},
            "achievement_reward_total_coins": 1, "achievement_rewards": {"synthetic": 1},
        },
        "read_my_cwl_placement": {"text": "Synthetic"},
        "read_my_cwl_channels": {"text": "Synthetic"},
        "read_my_examiner_profile": {"panel_channel_id": 100, "registered": True,
                                    "profile": {"th_levels": [18], "status": "Active",
                                                "timezone": "UTC", "availability": "Synthetic"},
                                    "profile_complete": True},
        "read_examiner_roster": {"panel_channel_id": 100, "total": 1, "offset": 0, "profiles": [{
            "id": 42, "name": "Synthetic", "ths": "18", "availability": "Synthetic",
            "status": "Active", "timezone": "UTC", "availability_raw": "Synthetic",
            "has_th_levels": True, "availability_valid": True, "profile_complete": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }]},
        "read_promotion_review": {"ticket_channel_id": 100, "review_channel_id": 101,
                                  "text": "Synthetic"},
        "read_role_connections": {
            "observed_at": "2026-01-01T00:00:00+00:00", "state_fingerprint": "synthetic",
            "total_entries": 1, "valid_rules": 1, "malformed_rule_count": 0,
            "malformed_rule_indexes": [], "cyclic_rule_count": 0,
            "evaluated_member": {"member_id": 42, "member_name": "Synthetic", "role_ids": [99]},
            "rules": [_render_rule(context, rule, frozenset({99}))], "next_offset": None,
            "complete_valid_rule_snapshot": True, "all_entries_valid": True,
        },
        "read_cwl_threads": {
            "threads": [{**asdict(synthetic_value(CwlThreadRegistration, seed)),
                         "url": "https://synthetic.invalid"}],
            "accessible_count": 1, "omitted_inaccessible_count": 0,
        },
        "cwl_ass_scores": ass, "cwl_bonus_scores": bonus,
        "search_approved_knowledge": {"sections": [{"title": "Synthetic", "body": "Synthetic",
                                                    "visibility": "public"}]},
        "remember_task_instruction": {"instruction": asdict(synthetic_value(TaskInstruction, seed)),
                                      "working_state_version": seed, "action_authorized": False},
        "retire_task_instruction": {"instruction_id": "synthetic", "active": False,
                                    "working_state_version": seed, "action_authorized": False},
        "prepare_spreadsheet": {"filename": "synthetic.xlsx", "sheets": 1, "rows": seed,
                                "attachment_prepared": False, "replaced_previous": True},
        "react_to_request": {"reacted": True}, "find_gif": {"url": "https://synthetic.invalid"},
    }


class AgentResultHintTests(unittest.TestCase):
    def assert_fields(self, shape, value, *, require_records=True):
        if isinstance(shape, dict):
            self.assertIsInstance(value, dict)
            for field, child in shape.items():
                key = field.removesuffix("?")
                if field.endswith("?") and key not in value:
                    continue
                self.assertIn(key, value)
                if value[key] is not None:
                    self.assert_fields(child, value[key], require_records=require_records)
        elif isinstance(shape, list):
            self.assertIsInstance(value, (list, tuple))
            if require_records:
                self.assertTrue(value, "Property fixtures must exercise returned records")
            for row in value:
                self.assert_fields(shape[0], row, require_records=require_records)

    def test_all_report_fields_match_real_pages_with_synthetic_records(self):
        for seed in range(1, 4):
            for kind in REPORT_FIELDS:
                with self.subTest(kind=kind, seed=seed):
                    self.assert_fields(REPORT_FIELDS[kind], report_page(kind, seed))

    def test_every_registered_hint_matches_synthetic_result_records(self):
        registry = build_agent_tools()
        for seed in range(1, 4):
            results = {name: model_result(value) for name, value in fixed_results(seed).items()}
            for name, kind in READ_KINDS.items():
                results[name] = report_page(kind, seed)
            for name, field in {
                "read_pending_transfer_requests": "selected_clan_code",
                "read_accessible_support_tickets": "selected_channel_id",
                "read_active_recruitment_trials": "selected_ticket_channel_id",
                "read_accessible_examination_cases": "selected_ticket_channel_id",
            }.items():
                results[name][field] = None
            for name, tool in registry.items():
                with self.subTest(capability=name, seed=seed):
                    self.assertTrue(tool.returns)
                    if name == "read_saved_report":
                        router = tool.handler.__wrapped__
                        self.assertEqual(set(router.specs), set(REPORT_FIELDS))
                        for kind in router.specs:
                            self.assertIn(kind + ":{" + format_returns(REPORT_FIELDS[kind]) + "}",
                                          tool.returns)
                            self.assert_fields(REPORT_FIELDS[kind], report_page(kind, seed))
                    elif name in results:
                        if name == "read_clash":
                            self.assertTrue(tool.returns.startswith(
                                format_returns(RESULT_FIELDS[name]),
                            ))
                        else:
                            self.assertEqual(tool.returns, format_returns(RESULT_FIELDS[name]))
                        self.assert_fields(RESULT_FIELDS[name], results[name])
                    else:
                        self.assertIs(tool.effect, AgentCapabilityEffect.COMMAND)
                        self.assertEqual(tool.returns, "status")
                        self.assert_fields({"status": ""}, {"status": "confirmation_required"})

    def test_dataclass_fields_are_not_handpicked(self):
        for value_type in (CwlAssWarsSnapshot, CwlBonusWarsSnapshot, TaskInstruction):
            with self.subTest(value_type=value_type):
                value = synthetic_value(value_type, 1)
                self.assertEqual(set(result_fields(value_type)), set(asdict(value)))

    def test_typed_dictionary_fields_include_nested_record_fields(self):
        shape = result_fields(SyntheticResult)
        self.assertEqual(format_returns(shape), "records:[{name,value}],complete")
        self.assert_fields(shape, {"records": [{"name": "Synthetic", "value": 1.125}],
                                   "complete": True})

    def test_clash_summary_hints_share_the_serializers_field_lists(self):
        def fake_payload(shape):
            if isinstance(shape, dict):
                return {key: fake_payload(value) for key, value in shape.items()}
            if isinstance(shape, list):
                return [fake_payload(shape[0])]
            return "Synthetic"

        hint = build_agent_tools()["read_clash"].returns
        for kind, shape in CLASH_SUMMARIES.items():
            with self.subTest(kind=kind):
                result = summary(kind, fake_payload(shape))
                self.assert_fields(shape, result)
                self.assertIn(kind + ":{" + format_returns(shape) + "}", hint)


class AgentResultHintExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_results_have_every_advertised_record_field(self):
        registry = build_agent_tools()
        with TemporaryDirectory() as directory:
            context = _context(directory)
            member = context.member
            member.name = "synthetic-handle"
            member.display_name = "Synthetic member"
            member.global_name = "Synthetic global name"
            member.bot = False
            member.mention = "<@42>"
            member.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for role in member.roles:
                role.name = "Synthetic role"
                role.is_default = lambda: False
            context.guild.members = [member]
            for name, arguments in (
                ("find_discord_members", {"query": "synthetic"}),
                ("read_discord_members", {"member_ids": [42], "include_roles": True}),
                ("read_discord_members", {"member_ids": [42], "include_roles": False}),
            ):
                with self.subTest(capability=name, arguments=arguments):
                    result = await registry[name].handler(context, arguments)
                    AgentResultHintTests().assert_fields(RESULT_FIELDS[name], result)

    async def test_member_read_and_spreadsheet_are_planned_before_any_results(self):
        registry = build_agent_tools()
        plan = {
            "goal": "Export synthetic members", "effort": "low", "output": "prepare_spreadsheet",
            "steps": [
                {"id": "lookup", "capability": "read_discord_members", "depends_on": [],
                 "arguments": {"member_ids": [42]}},
                {"id": "export", "capability": "prepare_spreadsheet", "depends_on": [],
                 "arguments": {"title": "Synthetic export", "sheets": [{
                     "name": "Members", "rows_from": {"step": "lookup", "path": ["members"]},
                     "columns": [{"field": "username", "heading": "Username"},
                                 {"field": "joined_at", "heading": "Joined"}],
                 }]}},
            ],
        }
        self.assertIn("username", registry["read_discord_members"].returns)
        self.assertIn("joined_at", registry["read_discord_members"].returns)
        self.assertTrue(check_plan(plan, registry).ok)
        with TemporaryDirectory() as directory:
            context = _context(directory)
            context.member.name = "synthetic-handle"
            context.member.display_name = "Synthetic member"
            context.member.joined_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

            async def run(step, arguments, earlier):
                return await registry[step["capability"]].handler(context, arguments)

            results = await execute_plan(plan, run, argument_schema=lambda step:
                                         registry[step["capability"]].definition.parameters)
            self.assertNotIn("error", results["lookup"])
            self.assertTrue(results["export"]["attachment_prepared"])
            workbook = load_workbook(BytesIO(context.state.attachments[0].data))
            try:
                self.assertEqual(list(workbook["Members"].values), [
                    ("Username", "Joined"), ("synthetic-handle", "2026-01-01T00:00:00+00:00"),
                ])
            finally:
                workbook.close()

    async def test_score_hints_match_real_combined_calculations(self):
        registry = build_agent_tools()
        dataset = cwl_fixtures._dataset()
        for attack in dataset["attacks"]:
            attack["player_name"] = "Synthetic player"
        queries = cwl_fixtures.CwlQueriesTests().bonus_queries(dataset)
        with TemporaryDirectory() as directory:
            context = replace(_context(directory), cwl_queries=queries)
            for name in ("cwl_ass_scores", "cwl_bonus_scores"):
                with self.subTest(capability=name), patch(
                    "elbow_helper.features.agent.access.has_access_requirements", return_value=True,
                ):
                    result = await registry[name].handler(context, {
                        "clan_code": "BEH", "war_ids": ["war-1", "war-2", "war-3"],
                    })
                    AgentResultHintTests().assert_fields(RESULT_FIELDS[name], result,
                                                        require_records=False)
