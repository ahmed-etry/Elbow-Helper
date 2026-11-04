"""Stored clan health datasets available to agent queries."""

from elbow_helper.features.clan_health.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "health", DB_PATH,
        tables=(
            (
                "raid_member_activity", "none",
                "One account in one capital raid weekend: attacks of attacks_expected; loot is "
                "capital gold. Filter on end_ts (UTC seconds).",
            ),
            (
                "player_directory", "none",
                "One row per account the bot has seen in a family clan: latest name, clan_code and "
                "townhall. first_seen_ts and last_seen_ts are UTC seconds when this bot observed "
                "it, not join dates or proof of new recruitment.",
            ),
            (
                "player_snapshots", "none",
                "One capture of one account at captured_ts (UTC seconds, about hourly; filter on it "
                "together with player_tag). hero_sum, pet_sum, equipment_sum, troop_sum and spell_sum "
                "add up levels across the category, not any one item's level; compare two captures "
                "to see upgrades. games_total is lifetime clan games points, not per season. "
                "Captures give no login time or who used the account.",
            ),
            (
                "report_runs", "none",
                "One health computation; partial is a collection-cycle flag, not clan coverage: "
                "judge a run by the clans and players its report_players rows hold, skipping runs "
                "with none or with missing clans; cycle bounds are UTC Unix seconds.",
            ),
            (
                "report_players", "none",
                "One account verdict per run_id; "
                "status Good/Watch/Needs Review/Insufficient data/Not tracked; "
                "status, flags_json and note are clan health's stored verdicts, not raw facts; "
                "flags_json contains grading flags; Good meets grading; Watch needs attention; "
                "Needs Review falls below thresholds; Insufficient data cannot grade; "
                "Not tracked excluded; deltas compare cycle snapshots.",
            ),
            (
                "clan_clan_health_config", "none",
                "One clan health override per clan; payload_json holds expectation blocks.",
            ),
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
