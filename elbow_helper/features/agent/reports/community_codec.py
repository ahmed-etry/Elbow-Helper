"""Reconstruct typed report data within its guild boundary."""
from typing import Any
from elbow_helper.features.achievements.queries import (
    AchievementProgressRow, MemberAchievementSnapshot,
)
from elbow_helper.features.event_stats.queries import (
    EventScheduleRow, EventScheduleSnapshot,
)
from elbow_helper.features.member_lifecycle.queries import (
    MemberLifecycleRow, MemberLifecycleSnapshot, OverdueApplicant,
)
from ..knowledge.store import KnowledgeSection
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
from ..capabilities.achievements.report import AchievementProgressReport
from ..capabilities.events.report import EventScheduleReport
from ..capabilities.member_lifecycle.report import MemberLifecycleReport
from ..knowledge.report import KnowledgeReport
from ..capabilities.examination.report import ExaminationCaseReport
from ..capabilities.hibernation.report import HibernationReport
from ..capabilities.recruitment.report import RecruitmentTrialReport
from ..research.report import DiscordResearchReport
from ..capabilities.support_tickets.report import SupportTicketReport
from .guild_boundary import require_guild


def decode_achievement_progress(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Achievement progress report")
    value = row["snapshot"]
    return AchievementProgressReport(
        row["report_id"], row["guild_id"], row["member_name"],
        MemberAchievementSnapshot(
            value["observed_at"], value["member_id"],
            value["completed_count"], value["total_count"],
            tuple(AchievementProgressRow(**item) for item in value["rows"]),
        ),
    )


def decode_event_schedule(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Event schedule report")
    value = row["snapshot"]
    return EventScheduleReport(
        row["report_id"], row["guild_id"], EventScheduleSnapshot(
            value["observed_at"],
            tuple(EventScheduleRow(**item) for item in value["rows"]),
        ),
    )


def decode_member_lifecycle(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Member lifecycle report")
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


def decode_approved_knowledge(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Approved knowledge report")
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


def decode_active_hibernation(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Hibernation report")
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


def decode_support_ticket_inventory(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Support ticket report")
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


def decode_active_recruitment_trials(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Recruitment trial report")
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


def decode_examination_case_status(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Examination case report")
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


def decode_discord_research(row: dict[str, Any], guild_id: int) -> Any:
    require_guild(row, guild_id, "Discord research report")
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
