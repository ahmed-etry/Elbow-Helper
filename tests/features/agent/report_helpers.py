"""Retained event reports for orchestration tests."""
from elbow_helper.features.agent.capabilities.events.report import EventScheduleReport
from elbow_helper.features.event_stats.queries import EventScheduleSnapshot


def make_event_report(report_id, created_at):
    created_at = "2026-01-01" if created_at == "now" else created_at
    if len(created_at) == 10:
        created_at += "T00:00:00+00:00"
    return EventScheduleReport(report_id, 1, EventScheduleSnapshot(created_at, ()))
