"""Stored rosters datasets available to agent queries."""

from elbow_helper.features.rosters.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "rosters",
        DB_PATH,
        tables=(
            (
                "rosters",
                "none",
                "One roster; status open/closed; active_cycle_id identifies current "
                "signups; recurring schedule times are local to schedule_utc_offset; "
                "one_off_open_ts and one_off_close_ts are UTC Unix seconds.",
            ),
            (
                "roster_cycles",
                "none",
                "One signup cycle per roster_id; opened_ts and closed_ts are UTC Unix seconds.",
            ),
            (
                "roster_members",
                "none",
                "One account signup per roster_id and cycle_id; discord_user_id owns "
                "the signup; signed_up_ts is UTC Unix seconds.",
            ),
        ),
        config_constant="elbow_helper.features.rosters.config.DB_PATH",
    ),
)
