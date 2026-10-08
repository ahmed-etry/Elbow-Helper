"""Queryable feature state files."""

from pathlib import Path

from elbow_helper.features.wars.config import CACHE_FILE

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "state",
        Path(CACHE_FILE),
        state_name="war_cache",
        level="none",
        note=(
            "War state: {processed_wars:[war_id],summary_messages:{<message_id>:{channel:int,"
            "sent_at:int}},war_board_messages:{<clan_code>:{channel:int,message:int}},"
            "war_board_history:{<clan_code>:{current:war,previous:war}},"
            "war_role_state:{<clan_code>:{lineup:{<player_tag>:member_id},managed:[member_id]}}}; "
            "sent_at Unix seconds UTC; war timestamps Clash UTC."
        ),
        config_constant="elbow_helper.features.wars.config.CACHE_FILE",
    ),
)
