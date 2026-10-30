"""Stored clan health datasets available to agent queries."""

from elbow_helper.features.clan_health.config import DB_PATH

from ...datasets.sources import DataSource


SOURCES = (
    DataSource(
        "health", DB_PATH,
        tables=(
            (
                "wars", "none",
                "One war of one family clan. war_type REG/CWL; state preparation/inWar/warEnded. "
                "Filter history on end_ts (UTC seconds); start_ts is battle day start. "
                "REG includes friendly wars: a war is friendly when start_ts - preparation_start_ts "
                "is under 82800 or end_ts - start_ts is under 86400 (Clash pauses only lengthen regular wars). Leave those out of regular war "
                "comparisons.",
            ),
            (
                "war_attacks", "none",
                "One attack by a family account. Filter on end_ts (UTC seconds, the war's end); "
                "join wars on war_id and clan_code. stars 0-3, destruction is percent, duration is "
                "seconds, fresh_attack is 0/1. defender_townhall is the target's town hall: compare "
                "attacks only against the attacker's town hall (war_roster_members.townhall, else "
                "the player_snapshots townhall at or before end_ts), never raw star averages. "
                "Regular-war attacks start in February but wars and war_roster_members cover regular "
                "wars only from mid-June 2026, so LEFT JOIN both; earlier attacks cannot be checked "
                "for friendly wars.",
            ),
            (
                "war_roster_members", "none",
                "One account on one war roster; townhall is its town hall in that war. "
                "captured_ts is UTC seconds.",
            ),
            (
                "war_activity", "none",
                "One account in one war: attacks_used of attacks_expected (missed = expected - used), "
                "stars and destruction summed over its attacks. Filter on end_ts (UTC seconds).",
            ),
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
                "One health computation; partial=1 means incomplete clan coverage; "
                "use partial=0 and latest created_ts for complete runs; "
                "cycle bounds are UTC Unix seconds.",
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
