"""Recruitment-local settings and persisted file paths."""

from __future__ import annotations

TRIAL_DAYS_DEFAULT = 7
TRIAL_DATA_FILE = "data/recruitment/trial_data.json"
TRIAL_REMINDERS_FILE = "data/recruitment/trial_reminders.json"
TRIAL_RESOLVED_REMINDER_RETENTION_HOURS = 12
REMINDER_HOURS = 18
TICKET_STATUS_ORDER = ["🚧", "🔗", "⛔", "⏳", "🤔", "✅"]
TICKET_RENAME_EMOJIS = ["🚧", "⏳", "🤔", "✅"]
TICKET_ACTIVITY_FILE = "data/recruitment/ticket_last_activity.json"
APPLICANT_AI_CLEANUP_HOURS = 12
APPLICANT_AI_FILE = "data/recruitment/applicant_messages.json"

TRIAL_TICKET_PREFIXES_START = [
    ("🚧ticket-", "⏳ticket-"),
]
TRIAL_TICKET_PREFIXES_END = [
    ("⏳ticket-", "🤔ticket-"),
]
DECLINED_TICKET_PREFIXES = [
    ("🚧ticket-", "⛔ticket-"),
    ("⏳ticket-", "⛔ticket-"),
    ("🤔ticket-", "⛔ticket-"),
    ("ticket-", "⛔ticket-"),
]
