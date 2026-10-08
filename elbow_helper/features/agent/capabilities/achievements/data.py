"""Stored achievements datasets available to agent queries."""

from pathlib import Path

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "achievements",
        Path("data/achievements/achievements.db"),
        tables=(
            (
                "achievements", "none",
                "One achievement definition; required_count is completion threshold.",
            ),
            (
                "user_achievements", "none",
                "One earned achievement per user_id; completed_date is text date.",
            ),
            (
                "user_coins", "lead",
                "One member coin balance and award checkpoints; last_daily_ts is UTC Unix seconds.",
            ),
            (
                "user_stats", "lead",
                "One member activity counter; voice durations are seconds except voice_hours; "
                "last_voice_join Unix seconds UTC; last_activity_date,early_bird_last_local_day,"
                "night_owl_last_local_day YYYYMMDD integers.",
            ),
            ("raffle_tickets", "lead_plus", "One member ticket; month_key=year*12+month."),
            (
                "raffle_winners", "lead_plus",
                "One draw; is_active marks retained winners; drawn_at is UTC Unix seconds; "
                "draw_type draw/reroll.",
            ),
            ("economy_meta", "lead_plus", "One economy setting key/value."),
            (
                "coin_transactions", "core",
                "One coin movement; amount signed; actor_id is Discord member ID; "
                "created_at is UTC Unix seconds.",
            ),
        ),
        config_constant="bot.paths.data_root / achievements / achievements.db",
    ),
)
