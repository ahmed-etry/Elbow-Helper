"""Typed reconstruction of conversation report artifacts from JSON objects."""

from typing import Any

from elbow_helper.features.rosters.models import (
    Roster, RosterCycle, RosterMember, RosterPost, RosterSnapshot,
)
from elbow_helper.features.achievements.queries import (
    AchievementProgressRow, MemberAchievementSnapshot,
)
from elbow_helper.features.achievements.economy_queries import (
    CoinTransactionRow, CoinTransactionSnapshot, RaffleSnapshot,
    RaffleWinnerRow,
)
from elbow_helper.features.event_stats.queries import (
    EventScheduleRow, EventScheduleSnapshot,
)
from elbow_helper.features.member_lifecycle.queries import (
    MemberLifecycleRow, MemberLifecycleSnapshot, OverdueApplicant,
)
from elbow_helper.features.clan_reporting.queries import (
    MissingElderRow, MissingElderSnapshot,
)
from ..knowledge.store import KnowledgeSection
from elbow_helper.features.cwl.queries import (
    CwlAssScopeRow, CwlAssScopeSnapshot, CwlBonusAttackScore,
    CwlBonusIneligiblePlayer, CwlBonusPlayerSummary, CwlBonusScopeSnapshot,
    CwlBonusSettings, CwlClanSeasonSummary, CwlPerformanceRow,
    CwlPerformanceSnapshot,
)
from elbow_helper.features.clan_health.queries import (
    ClanHealthPlayerRow, ClanHealthReportRun, ClanHealthReportSnapshot,
    CompleteFamilySnapshotRun, FamilyAccountMovement, FamilyMovementHistory,
    FamilySnapshotInterval, HistoricalRegularWar,
    HistoricalRegularWarHistory, HistoricalRegularWarMember,
)
from elbow_helper.features.wars.queries import RegularWarSnapshot, WarMemberEvidence
from elbow_helper.features.clan_transfers.queries import (
    PendingTransferRequest, TransferQueueSnapshot,
)
from elbow_helper.features.hibernation.queries import (
    ActiveHibernationRecord, ActiveHibernationSnapshot,
)
from elbow_helper.features.support_tickets.queries import (
    SupportTicketMetadata, SupportTicketSnapshot,
)
from elbow_helper.features.recruitment.queries import (
    ActiveRecruitmentTrial, ActiveRecruitmentTrialSnapshot,
)
from elbow_helper.features.examination.queries import (
    ExaminationCaseSnapshot, ExaminationCaseStatus,
)
from elbow_helper.features.records.queries import (
    LeadershipRecordRow, LeadershipRecordSnapshot,
)

from ..files.contracts import (
    CsvImportArtifact,
    TextImportArtifact,
    XlsxImportArtifact,
    XlsxSheet,
)
from .achievement import (
    AchievementLeaderboardMember, AchievementLeaderboardReport,
    AchievementProgressReport, CoinTransactionReport, RaffleReport,
)
from .event import EventScheduleReport
from .member_lifecycle import MemberLifecycleReport
from .clan_reporting import MissingElderReport
from .knowledge import KnowledgeReport
from .clan_health import ClanHealthReport
from .cwl import CwlAssScopeReport, CwlPerformanceReport
from .cwl_bonus import CwlBonusScopeReport
from .examination import ExaminationCaseReport
from .hibernation import HibernationReport
from .historical_war import (
    HistoricalRegularWarReport, HistoricalWarOwnedMember,
)
from .leadership_record import LeadershipRecordReport
from .movement import FamilyMovementReport, OwnedFamilyAccountMovement
from .recruitment import RecruitmentTrialReport
from .roles import RoleAccountReport
from .research import DiscordResearchReport
from .roster import RosterReport
from .support import SupportTicketReport
from .transfer import TransferQueueReport
from .war import RegularWarReport


def decode_report(row: dict[str, Any], *, guild_id: int) -> Any:
    """Reconstruct one typed report while enforcing its guild boundary."""
    kind = row["kind"]
    if kind == "achievement_progress":
        _require_guild(row, guild_id, "Achievement progress report")
        value = row["snapshot"]
        return AchievementProgressReport(
            row["report_id"], row["guild_id"], row["member_name"],
            MemberAchievementSnapshot(
                value["observed_at"], value["member_id"],
                value["completed_count"], value["total_count"],
                tuple(AchievementProgressRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "achievement_leaderboard":
        _require_guild(row, guild_id, "Achievement leaderboard report")
        return AchievementLeaderboardReport(
            row["report_id"], row["guild_id"], row["observed_at"],
            row["total_achievements"],
            tuple(AchievementLeaderboardMember(**item) for item in row["rows"]),
        )
    if kind == "coin_transactions":
        _require_guild(row, guild_id, "Coin transaction report")
        value = row["snapshot"]
        return CoinTransactionReport(
            row["report_id"], row["guild_id"], row["member_name"],
            CoinTransactionSnapshot(
                value["observed_at"], value["member_id"],
                value["total_transactions"],
                tuple(CoinTransactionRow(**item) for item in value["rows"]),
                value["complete"],
            ),
        )
    if kind == "raffle":
        _require_guild(row, guild_id, "Raffle report")
        value = row["snapshot"]
        return RaffleReport(
            row["report_id"], row["guild_id"], RaffleSnapshot(
                value["observed_at"], value["month_key"], value["month_label"],
                value["prize"], value["configured_winners"],
                value["total_tickets"], tuple(value["ticket_member_ids"]),
                value["total_winner_rows"],
                tuple(RaffleWinnerRow(**item) for item in value["winners"]),
                value["complete"],
            ),
            tuple(tuple(item) for item in row["member_names"]),
        )
    if kind == "event_schedule":
        _require_guild(row, guild_id, "Event schedule report")
        value = row["snapshot"]
        return EventScheduleReport(
            row["report_id"], row["guild_id"], EventScheduleSnapshot(
                value["observed_at"],
                tuple(EventScheduleRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "member_lifecycle":
        _require_guild(row, guild_id, "Member lifecycle report")
        value = row["snapshot"]
        return MemberLifecycleReport(
            row["report_id"], row["guild_id"], row["source_channel_id"],
            MemberLifecycleSnapshot(
                observed_at=value["observed_at"],
                stored_member_entry_count=value["stored_member_entry_count"],
                current_guild_member_count=value["current_guild_member_count"],
                skipped_invalid_member_entries=value[
                    "skipped_invalid_member_entries"
                ],
                untracked_current_member_count=value[
                    "untracked_current_member_count"
                ],
                skipped_invalid_platform_counts=value[
                    "skipped_invalid_platform_counts"
                ],
                skipped_invalid_overdue_entries=value[
                    "skipped_invalid_overdue_entries"
                ],
                last_weekly_report_at=value["last_weekly_report_at"],
                last_applicant_scan_at=value["last_applicant_scan_at"],
                platform_counts=tuple(
                    (item[0], item[1]) for item in value["platform_counts"]
                ),
                overdue_applicants=tuple(
                    OverdueApplicant(**item)
                    for item in value["overdue_applicants"]
                ),
                rows=tuple(MemberLifecycleRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "missing_elder":
        _require_guild(row, guild_id, "Missing Elder report")
        value = row["snapshot"]
        return MissingElderReport(
            row["report_id"], row["guild_id"],
            tuple(row["source_channel_ids"]),
            tuple(row["omitted_inaccessible_clan_codes"]),
            MissingElderSnapshot(
                observed_at=value["observed_at"],
                selected_clan_codes=tuple(value["selected_clan_codes"]),
                skipped_invalid_row_count=value["skipped_invalid_row_count"],
                rows=tuple(MissingElderRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "approved_knowledge":
        _require_guild(row, guild_id, "Approved knowledge report")
        return KnowledgeReport(
            row["report_id"], row["guild_id"], row["observed_at"],
            row["query"], tuple(row["topics"]),
            tuple(KnowledgeSection(**{
                **item,
                "topics": tuple(item["topics"]),
                "source_refs": tuple(item["source_refs"]),
                "conflicts_with": tuple(item["conflicts_with"]),
            }) for item in row["sections"]),
        )
    if kind == "role_accounts":
        return RoleAccountReport(
            row["report_id"], row["created_at"], tuple(row["roles"]),
            tuple(row["members"]), row.get("parent_report_id"),
            tuple(row.get("refresh_attempted_player_tags", ())),
        )
    if kind == "roster_signups":
        value = row["snapshot"]
        roster = Roster(**value["roster"])
        if roster.guild_id != guild_id:
            raise ValueError("Roster report belongs to another guild")
        if value["cycle"] and value["cycle"]["roster_id"] != roster.id:
            raise ValueError("Roster report cycle belongs to another roster")
        if any(post["roster_id"] != roster.id for post in value["posts"]):
            raise ValueError("Roster report post belongs to another roster")
        return RosterReport(row["report_id"], RosterSnapshot(
            roster, RosterCycle(**value["cycle"]) if value["cycle"] else None,
            tuple(RosterMember(**member) for member in value["members"]),
            tuple(RosterPost(**post) for post in value["posts"]),
            value["observed_ts"],
        ))
    if kind == "cwl_performance":
        _require_guild(row, guild_id, "CWL performance report")
        value = row["snapshot"]
        return CwlPerformanceReport(
            row["report_id"], row["guild_id"], CwlPerformanceSnapshot(
                value["observed_at"], value["history_limit"],
                tuple(value["seasons"]),
                tuple(CwlClanSeasonSummary(**item)
                      for item in value["clan_seasons"]),
                tuple(CwlPerformanceRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "cwl_ass_scope":
        _require_guild(row, guild_id, "Scoped CWL ASS report")
        value = row["snapshot"]
        return CwlAssScopeReport(
            row["report_id"], row["guild_id"], CwlAssScopeSnapshot(
                observed_at=value["observed_at"],
                clan_code=value["clan_code"], season=value["season"],
                scope_type=value["scope_type"],
                requested_round=value["requested_round"],
                requested_war_id=value["requested_war_id"],
                resolved_war_ids=tuple(value["resolved_war_ids"]),
                resolved_rounds=tuple(value["resolved_rounds"]),
                completed_wars=value["completed_wars"],
                league=value["league"], profile_key=value["profile_key"],
                profile_label=value["profile_label"],
                difficulty_weight=value["difficulty_weight"],
                missed_mode=value["missed_mode"],
                scoring_status=value["scoring_status"],
                coverage_status=value["coverage_status"],
                rows=tuple(CwlAssScopeRow(**item) for item in value["rows"]),
            ),
        )
    if kind == "cwl_bonus_scope":
        _require_guild(row, guild_id, "CWL bonus scope report")
        value = row["snapshot"]
        return CwlBonusScopeReport(
            row["report_id"], row["guild_id"], CwlBonusScopeSnapshot(
                observed_at=value["observed_at"],
                clan_code=value["clan_code"], season=value["season"],
                scope_type=value["scope_type"],
                requested_round=value["requested_round"],
                requested_war_tag=value["requested_war_tag"],
                resolved_rounds=tuple(value["resolved_rounds"]),
                resolved_war_tags=tuple(value["resolved_war_tags"]),
                settings=CwlBonusSettings(**value["settings"]),
                summaries=tuple(
                    CwlBonusPlayerSummary(**item)
                    for item in value["summaries"]
                ),
                ineligible=tuple(
                    CwlBonusIneligiblePlayer(**item)
                    for item in value["ineligible"]
                ),
                attacks=tuple(
                    CwlBonusAttackScore(**item) for item in value["attacks"]
                ),
                warnings=tuple(value["warnings"]),
                coverage_status=value["coverage_status"],
            ),
        )
    if kind == "clan_health":
        _require_guild(row, guild_id, "Clan-health report")
        value = row["snapshot"]
        return ClanHealthReport(
            row["report_id"], row["guild_id"], ClanHealthReportSnapshot(
                run=ClanHealthReportRun(**value["run"]),
                clan_code=value["clan_code"],
                rows=tuple(ClanHealthPlayerRow(
                    **{**item, "flags": tuple(item["flags"])}
                ) for item in value["rows"]),
                issues=tuple(value["issues"]),
            ),
        )
    if kind == "regular_war":
        _require_guild(row, guild_id, "Regular-war report")
        value = row["snapshot"]
        return RegularWarReport(
            row["report_id"], row["guild_id"], RegularWarSnapshot(
                **{
                    **value,
                    "members": tuple(WarMemberEvidence(**item)
                                     for item in value["members"]),
                    "issues": tuple(value["issues"]),
                },
            ),
        )
    if kind == "historical_regular_wars":
        _require_guild(row, guild_id, "Historical regular-war report")
        value = row["history"]
        history = HistoricalRegularWarHistory(
            read_at=value["read_at"], clan_code=value["clan_code"],
            wars=tuple(HistoricalRegularWar(
                **{**item, "issues": tuple(item["issues"])}
            ) for item in value["wars"]),
            members=tuple(HistoricalRegularWarMember(**item)
                          for item in value["members"]),
            next_before_war_id=value["next_before_war_id"],
        )
        return HistoricalRegularWarReport(
            row["report_id"], row["guild_id"], row["ownership_observed_at"],
            history, tuple(HistoricalWarOwnedMember(
                source=HistoricalRegularWarMember(**item["source"]),
                linked_member_id=item["linked_member_id"],
                linked_player_name=item["linked_player_name"],
            ) for item in row["rows"]),
        )
    if kind == "family_account_movements":
        _require_guild(row, guild_id, "Family movement report")
        value = row["history"]
        history = FamilyMovementHistory(
            read_at=value["read_at"],
            runs=tuple(CompleteFamilySnapshotRun(
                **{**item, "ambiguous_account_tags": tuple(
                    item["ambiguous_account_tags"]
                )}
            ) for item in value["runs"]),
            intervals=tuple(FamilySnapshotInterval(
                **{**item, "excluded_ambiguous_account_tags": tuple(
                    item["excluded_ambiguous_account_tags"]
                )}
            ) for item in value["intervals"]),
            movements=tuple(FamilyAccountMovement(**item)
                            for item in value["movements"]),
            next_before_run_id=value["next_before_run_id"],
        )
        return FamilyMovementReport(
            row["report_id"], row["guild_id"], row["ownership_observed_at"],
            history, tuple(OwnedFamilyAccountMovement(
                source=FamilyAccountMovement(**item["source"]),
                linked_member_id=item["linked_member_id"],
                linked_player_name=item["linked_player_name"],
            ) for item in row["rows"]),
        )
    if kind == "pending_transfer_requests":
        _require_guild(row, guild_id, "Pending transfer report")
        return TransferQueueReport(
            row["report_id"], row["guild_id"], row["observed_at"],
            row["request_ttl_hours"], row["registered_queue_count"],
            row["omitted_inaccessible_count"],
            tuple(TransferQueueSnapshot(
                clan_code=item["clan_code"], thread_id=item["thread_id"],
                stored_request_count=item["stored_request_count"],
                expired_stored_count=item["expired_stored_count"],
                pending=tuple(PendingTransferRequest(**request)
                              for request in item["pending"]),
            ) for item in row["queues"]),
        )
    if kind == "active_hibernation":
        _require_guild(row, guild_id, "Hibernation report")
        value = row["snapshot"]
        return HibernationReport(
            row["report_id"], row["guild_id"], row["source_channel_id"],
            ActiveHibernationSnapshot(
                observed_at=value["observed_at"],
                stored_member_entry_count=value["stored_member_entry_count"],
                skipped_invalid_member_entries=value[
                    "skipped_invalid_member_entries"
                ],
                ignored_metadata_entries=value["ignored_metadata_entries"],
                missing_start_time_count=value["missing_start_time_count"],
                records=tuple(ActiveHibernationRecord(**item)
                              for item in value["records"]),
            ),
        )
    if kind == "support_ticket_inventory":
        _require_guild(row, guild_id, "Support ticket report")
        value = row["snapshot"]
        return SupportTicketReport(
            row["report_id"], row["guild_id"],
            row["registered_ticket_count"], row["omitted_inaccessible_count"],
            SupportTicketSnapshot(
                observed_at=value["observed_at"],
                tickets=tuple(SupportTicketMetadata(**item)
                              for item in value["tickets"]),
            ),
        )
    if kind == "active_recruitment_trials":
        _require_guild(row, guild_id, "Recruitment trial report")
        value = row["snapshot"]
        return RecruitmentTrialReport(
            row["report_id"], row["guild_id"], ActiveRecruitmentTrialSnapshot(
                observed_at=value["observed_at"],
                selected_entry_count=value["selected_entry_count"],
                skipped_invalid_selected_count=value[
                    "skipped_invalid_selected_count"
                ],
                trials=tuple(ActiveRecruitmentTrial(**item)
                             for item in value["trials"]),
            ),
        )
    if kind == "examination_case_status":
        _require_guild(row, guild_id, "Examination case report")
        value = row["snapshot"]
        return ExaminationCaseReport(
            row["report_id"], row["guild_id"], ExaminationCaseSnapshot(
                observed_at=value["observed_at"],
                selected_entry_count=value["selected_entry_count"],
                skipped_invalid_selected_count=value[
                    "skipped_invalid_selected_count"
                ],
                cases=tuple(ExaminationCaseStatus(**item)
                            for item in value["cases"]),
            ),
        )
    if kind == "active_leadership_records":
        _require_guild(row, guild_id, "Leadership record report")
        value = row["snapshot"]
        return LeadershipRecordReport(
            row["report_id"], row["guild_id"], LeadershipRecordSnapshot(
                observed_at=value["observed_at"], member_id=value["member_id"],
                records=tuple(LeadershipRecordRow(**item)
                              for item in value["records"]),
            ),
        )
    if kind in {"csv_import", "xlsx_import", "text_import"}:
        return _decode_attachment_report(row, guild_id=guild_id)
    if kind == "discord_research":
        _require_guild(row, guild_id, "Discord research report")
        return DiscordResearchReport(
            report_id=row["report_id"], guild_id=row["guild_id"],
            source_job_id=row["source_job_id"],
            source_channel_id=row["source_channel_id"],
            research_kind=row["research_kind"], query=row["query"],
            author_id=row["author_id"], after=row["after"], before=row["before"],
            status=row["status"], pages_completed=row["pages_completed"],
            total_results_estimate=row["total_results_estimate"],
            coverage=row["coverage"], messages=tuple(row["messages"]),
            finished_at=row["finished_at"],
        )
    raise ValueError("Unsupported conversation report kind")


def _decode_attachment_report(row: dict[str, Any], *, guild_id: int) -> Any:
    common = {
        "report_id": row["report_id"], "guild_id": row["guild_id"],
        "channel_id": row["channel_id"], "message_id": row["message_id"],
        "attachment_id": row["attachment_id"], "filename": row["filename"],
        "content_type": row["content_type"], "byte_size": row["byte_size"],
        "sha256": row["sha256"], "imported_at": row["imported_at"],
        "parser_version": row["parser_version"],
    }
    if row["kind"] == "csv_import":
        report = CsvImportArtifact(
            **common, encoding=row["encoding"], delimiter=row["delimiter"],
            columns=tuple(row["columns"]),
            rows=tuple(tuple(item) for item in row["rows"]),
            issues=tuple(row["issues"]),
        )
    elif row["kind"] == "xlsx_import":
        report = XlsxImportArtifact(
            **common, uncompressed_size=row["uncompressed_size"],
            sheets=tuple(XlsxSheet(
                item["name"], tuple(item["columns"]),
                tuple(tuple(value) for value in item["rows"]),
                tuple(item["issues"]),
            ) for item in row["sheets"]),
        )
    else:
        report = TextImportArtifact(
            **common, encoding=row["encoding"],
            document_format=row["document_format"], text=row["text"],
            line_count=row["line_count"],
        )
    if report.guild_id != guild_id:
        label = {
            "csv_import": "CSV import",
            "xlsx_import": "XLSX import",
            "text_import": "Text import",
        }[row["kind"]]
        raise ValueError(f"{label} belongs to another guild")
    return report










def _require_guild(row: dict[str, Any], guild_id: int, label: str) -> None:
    if row["guild_id"] != guild_id:
        raise ValueError(f"{label} belongs to another guild")


__all__ = ["decode_report"]
