"""Stored clan health datasets available to agent queries."""

from elbow_helper.features.clan_health.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "health", DB_PATH,
        tables=(
            (
                "wars", "none",
                "One war; war_type REG/CWL/UNKNOWN; state preparation/inWar/warEnded/notInWar; "
                "source=war_cwl_api/currentwar_api:<state>/currentwar_api:warEnded_scheduled/"
                "cwl_api:warEnded_scheduled; use end_ts for history.",
            ),
            (
                "war_attacks", "none",
                "One attack per war; destruction is percent; duration is seconds; "
                "fresh_attack is boolean; source=war_cwl_api/currentwar_api:<state>/"
                "currentwar_api:warEnded_scheduled/cwl_api:warEnded_scheduled.",
            ),
            (
                "war_roster_members", "none",
                "One account per war roster; roster_state inWar/warEnded; "
                "source=war_cwl_api/currentwar_api:<state>/currentwar_api:warEnded_scheduled/"
                "cwl_api:warEnded_scheduled.",
            ),
            (
                "war_activity", "none",
                "One account per war; attacks_used and attacks_expected summarize participation; "
                "source=war_cwl_api/currentwar_api:<state>/currentwar_api:warEnded_scheduled/"
                "cwl_api:warEnded_scheduled.",
            ),
            (
                "raid_member_activity", "none",
                "One account per raid weekend; loot is capital gold; "
                "source=capitalraidseasons_api.",
            ),
            (
                "player_directory", "lead_plus",
                "Latest known account identity; first_seen_ts and last_seen_ts "
                "are UTC Unix seconds.",
            ),
            (
                "player_snapshots", "lead_plus",
                "One periodic account snapshot at captured_ts; sums are total upgrade levels; "
                "games_total is lifetime games points.",
            ),
            (
                "report_runs", "lead_plus",
                "One health computation; partial=1 means incomplete clan coverage; "
                "use partial=0 and latest created_ts for complete runs; "
                "cycle bounds are UTC Unix seconds.",
            ),
            (
                "report_players", "lead_plus",
                "One account verdict per run_id; "
                "status Good/Watch/Needs Review/Insufficient data/Not tracked; "
                "status, flags_json and note are clan health's stored verdicts, not raw facts; "
                "flags_json contains grading flags; Good meets grading; Watch needs attention; "
                "Needs Review falls below thresholds; Insufficient data cannot grade; "
                "Not tracked excluded; deltas compare cycle snapshots.",
            ),
            (
                "clan_clan_health_config", "lead_plus",
                "One clan health override per clan; payload_json holds expectation blocks.",
            ),
            (
                "clan_player_health_config", "lead_plus",
                "One player health override per clan; payload_json holds expectation blocks.",
            ),
            (
                "clan_health_config_audit", "lead_plus",
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
        state_name="player_health_config", level="lead_plus",
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
