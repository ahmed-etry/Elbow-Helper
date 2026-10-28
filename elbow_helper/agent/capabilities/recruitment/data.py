"""Recruitment workflow state, restricted to accessible ticket channels."""

from pathlib import Path

from elbow_helper.features.recruitment.config import (
    TICKET_ACTIVITY_FILE, TRIAL_DATA_FILE, TRIAL_REMINDERS_FILE,
)

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "state", Path(TRIAL_DATA_FILE), state_name="recruitment_trials",
        level="recruitment", channel_keyed=True,
        note=(
            "Active trial registrations keyed by ticket channel ID: "
            "{<ticket_id>:{applicant_id:int,start:ISO_UTC,days:int,"
            "tracking_msg_id:int,tracking_channel_id:int}}. "
            "Only accessible tickets are included. /accept starts a trial; "
            "the trial-list tracking post includes Clans. Read that post and the "
            "ticket acceptance message to verify placement. Entries are removed "
            "on expiry, trial end or departure; absence does not prove no acceptance."
        ),
        config_constant="elbow_helper.features.recruitment.config.TRIAL_DATA_FILE",
    ),
    DataSource(
        "state", Path(TRIAL_REMINDERS_FILE), state_name="recruitment_trial_reminders",
        level="recruitment", channel_keyed=True,
        note=(
            "Trial-end reminders keyed by accessible ticket channel ID: "
            "{<ticket_id>:{channel_id:int,message_id:int,applicant_id:int,"
            "created_at:Unix_s_UTC,resolved_at:Unix_s_UTC|null,resolved_by:int|null}}. "
            "Unresolved means trial period elapsed and the end action is pending. "
            "Resolved records the trial-end action and member follow-up, not a "
            "new accept/decline decision or finalized membership. Check the ticket "
            "for delivery, reply and /finalize. Resolved reminders are deleted "
            "after retention; this is not a complete historical ledger."
        ),
        config_constant="elbow_helper.features.recruitment.config.TRIAL_REMINDERS_FILE",
    ),
    DataSource(
        "state", Path(TICKET_ACTIVITY_FILE), state_name="recruitment_ticket_activity",
        level="recruitment", channel_keyed=True,
        note=(
            "Unanswered-applicant reminders keyed by accessible ticket channel ID: "
            "{<ticket_id>:{last_reminder_at:ISO_UTC,message_id:int|null,"
            "message_channel_id:int|null,last_applicant_message_id:int|null}}. "
            "These are 18-hour response reminders, separate from trial-end "
            "follow-ups; not acceptance, placement or completion records."
        ),
        config_constant="elbow_helper.features.recruitment.config.TICKET_ACTIVITY_FILE",
    ),
)
