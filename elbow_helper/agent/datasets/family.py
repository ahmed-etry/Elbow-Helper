"""Neutral family-record facts available to read-only SQL."""

from elbow_helper.configuration.files import FAMILY_DATABASE

from .sources import DataSource


SOURCES = (
    DataSource(
        "family", FAMILY_DATABASE,
        tables=(
            ("wars", "none",
             "One war, shared by both sides. type REG/CWL/FRIENDLY; friendly wars are separate. "
             "Filter completed history with state='warEnded' and end_at (UTC microseconds); "
             "start_at is battle start. type_basis describes classification certainty. "
             "Completeness flags alone do not prove a side's missed attacks; use war_members' checks."),
            ("war_sides", "none",
             "One clan side per war; join wars.id=war_id and clans.tag=clan_tag (is_family=1 "
             "selects family sides). Join members on BOTH war_id and side. Filter via wars.end_at "
             "(UTC microseconds). stars and attacks are side totals, destruction is percent. "
             "extra_json.derived lists totals computed from available rows, not independent evidence."),
            ("war_members", "none",
             "One account per war side, including opponents; id joins attack member IDs. "
             "Filter via wars.end_at (UTC microseconds). townhall is the attacker's town hall in that war. "
             "Misses=max(0,attacks_expected-attacks_used) only for state='warEnded' with complete side "
             "counts: member count=team_size, all expected/used non-NULL, non-stub stored attack "
             "count=war_sides.attacks, each used count matches stored outgoing attacks, "
             "no wholly unattributed attacks, and extra_json.derived on "
             "the side excludes 'attacks'. Otherwise misses are unknown. account_id may be NULL; "
             "player_tag also joins accounts.player_tag or links.links.player_tag."),
            ("war_attacks", "none",
             "One attack per (war_id,attack_order). Filter via wars.end_at (UTC microseconds). "
             "Join attacker_member_id and defender_member_id separately to war_members.id; LEFT JOIN "
             "defenders. Exclude COALESCE(json_extract(extra_json,'$.stub'),0)=1. stars 0-3, "
             "destruction percent, duration seconds. Fair comparisons use defender townhall against "
             "the attacker's town hall, not raw star averages. Defender TH is "
             "COALESCE(defender.townhall,json_extract(attack.extra_json,'$.legacy.defender_townhall')); "
             "unknown TH cannot establish an equal match."),
            ("accounts", "none",
             "One known Clash account; id is internal, player_tag joins health snapshots and links.links. "
             "first_observed_at and last_profile_at are UTC microseconds, not join dates. "
             "LEFT JOIN account_links ON account_id=id AND to_at IS NULL, then members ON "
             "members.id=member_id for discord_id; unlinked accounts remain with NULL owner."),
            ("account_links", "none",
             "One observed account-to-member ownership interval. Join account_id to accounts.id and "
             "member_id to members.id (discord_id is the Discord ID). from_at/to_at are UTC "
             "microseconds; to_at IS NULL selects current links. For dated links use from_at<=:at "
             "AND (to_at IS NULL OR to_at>:at). Observation bounds do not prove exact change times."),
            ("members", "none",
             "One observed Discord member; id is internal, discord_id is the Discord member ID. "
             "first_observed_at/last_observed_at are UTC microseconds, not server join dates."),
            ("clans", "none",
             "One clan identity; id joins clan_membership.clan_id, tag joins war_sides.clan_tag. "
             "is_family=1 selects family clans; family_code is the configured clan code. No time column."),
            ("clan_membership", "none",
             "One observed account-in-clan interval; join account_id=accounts.id and clan_id=clans.id. "
             "from_at/to_at/last_observed_at are UTC microseconds. to_at IS NULL selects current "
             "membership; from_at<=:at AND (to_at IS NULL OR to_at>:at) selects a dated interval. "
             "Gaps are unknown, and first observation is not a verified join date."),
            ("cwl_seasons", "none",
             "One league group per (season_key,group_key); id joins season_id. Filter season_key "
             "(YYYY-MM or a returned key); one month can have several groups. "
             "first_seen_at/final_at are UTC microseconds."),
            ("cwl_rounds", "none",
             "One war slot per (season_id,round_no,war_tag); round_no is CWL day 1-7. "
             "Join season_id=cwl_seasons.id and war_id=wars.id; unrevealed wars can have NULL war_id. "
             "Filter via season_key or wars.end_at (UTC microseconds)."),
            ("cwl_clans", "none",
             "One clan per league group; join season_id=cwl_seasons.id and clan_tag=clans.tag. "
             "Filter via cwl_seasons.season_key; no independent time column."),
            ("cwl_players", "none",
             "One account on a season's clan roster, NOT a battle-day lineup. Join on season_id "
             "AND clan_tag; player_tag joins accounts or links.links, account_id may be NULL. "
             "Filter via cwl_seasons.season_key; use war_members for actual participation."),
            ("cwl_attacks", "none",
             "One CWL attack per (season_id,round_no,war_id,attack_order), with war_attacks columns. "
             "Filter season_id via cwl_seasons.season_key or join wars.id=war_id for end_at "
             "(UTC microseconds). Use war_attacks' member joins, stub exclusion and town hall checks. "
             "This view reads cwl_rounds, wars and war_attacks; do not join rounds again and multiply rows."),
        ),
        config_constant="elbow_helper.configuration.files.FAMILY_DATABASE",
    ),
)
