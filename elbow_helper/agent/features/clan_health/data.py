"""Clan health settings available to agent queries; activity facts are in the family record's reads."""

from elbow_helper.features.clan_health.config import DB_PATH

from elbow_helper.agent.reads.sql.sources import DataSource


SOURCES = (
    DataSource(
        "health", DB_PATH,
        tables=(
            (
                "clan_player_health_config", "none",
                "One player health override per clan; payload_json holds expectation blocks.",
            ),
            (
                "clan_health_config_audit", "none",
                "One configuration edit; actor_discord_id is the member who edited; "
                "ts_utc is ISO UTC.",
            ),
        ),
        config_constant="elbow_helper.features.clan_health.config.DB_PATH",
    ),
)

from elbow_helper.features.clan_health.player_health_config import PLAYER_HEALTH_CONFIG_FILE

SOURCES += (
    DataSource(
        "state", PLAYER_HEALTH_CONFIG_FILE,
        state_name="player_health_config", level="none",
        note=(
            "{profiles:{<profile_name>:{war:{wars_to_join:int,missed_attack_rate_percent:number},"
            "raids:{minimum_capital_gold_per_event:int},"
            "clan_games:{minimum_points_per_event:int}}}}; player expectations."
        ),
        config_constant=(
            "elbow_helper.features.clan_health.player_health_config.PLAYER_HEALTH_CONFIG_FILE"
        ),
    ),
)
