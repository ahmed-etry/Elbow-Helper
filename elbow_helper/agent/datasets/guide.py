"""Cache access-specific guides from feature schemas at process setup."""

from contextlib import closing
import sqlite3

from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from elbow_helper.configuration.channels import TRIAL_LIST, TICKETS_LOG

from .catalogue import SOURCES


HEADER = """\
SQLite dialect. Name tables alias.table. State files are state.name with one JSON column doc; \
use json_each/json_extract. Pass lists as one named parameter: \
IN (SELECT value FROM json_each(:name)). Columns ending _ts are Unix seconds UTC \
(datetime(x, 'unixepoch')); _utc and _at text columns are ISO 8601 unless noted otherwise.
family integer time columns are UTC microseconds: datetime(x/1000000, 'unixepoch').
Outside family, member_id, discord_user_id and user_id are Discord member IDs. player_tag is an uppercase \
Clash account tag (#...). clan_code is a family clan code. One member can link many accounts; \
family.account_links joins accounts.id to members.id; members.discord_id is the Discord ID; \
to_at IS NULL selects current ownership. links.links also maps account tags to discord_user_id. \
Join accounts across family, links, health and rosters by \
player_tag. PK1, PK2 and subsequent markers identify a table's complete primary key in order. \
Keep the full record identity when joining related rows; matching one part of a composite \
key is insufficient. Aggregate each one-to-many input before joining totals. Return one short \
row per account or member and set max_rows to 1000 for per-account results; a result that was \
cut says how many rows were left out and is not the full answer."""


class DataGuide:
    def __init__(self, paths, sources=SOURCES):
        self.entries = []
        self.variants = {}
        for source in sources:
            path = source.resolve(paths)
            if not path.is_file():
                continue
            if source.state_name:
                self.entries.append((
                    source.level,
                    f"state.{source.state_name} [{source.level}]: {source.note}; doc TEXT",
                ))
                continue
            try:
                with closing(sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True,
                )) as connection:
                    for table, level, note in source.tables:
                        columns = connection.execute(f"PRAGMA table_info(\"{table}\")").fetchall()
                        if columns:
                            self.entries.append((
                                level,
                                f"{source.alias}.{table} [{level}]: {note}\n"
                                + ",".join(_column(row, source.alias == "family") for row in columns),
                            ))
            except sqlite3.Error:
                continue
        self.clans = "\n".join(
            f"{code}: {CLANS[code].name}, {CLANS[code].tag}, utility={CLANS[code].is_utility}, "
            f"member={CLANS[code].member_role_id}, war={CLANS[code].war_role_id}, "
            f"CWL={CLANS[code].cwl_role_id}, leadership={CLANS[code].leadership_role_id}"
            for code in CLAN_ORDER
        )

    def for_levels(self, levels):
        key = frozenset(levels)
        if key not in self.variants:
            entries = [text for level, text in self.entries if level == "none" or level in key]
            unavailable = [
                text.split(":", 1)[0] for level, text in self.entries
                if level != "none" and level not in key
            ]
            self.variants[key] = "\n".join([
                HEADER,
                "Live Clash: read_clash kinds clan (memberList, levels, leagues), "
                "player (compact profile), current_war/cwl_war (scores and compact rosters), "
                "cwl_group (season, clans, rounds.warTags), "
                "war_log (items with results, stars, destruction), "
                "current_war, war_log, cwl_group and cwl_war only cover clans the family record does not collect; "
                "use read_current_war/read_war_attendance/read_cwl_results for family clans. "
                "battle_log (latest player attacks/defenses), "
                "capital_raids (items without attackLog/defenseLog). "
                "Defaults omit heroes, troops and other bulk fields. fields replaces defaults with "
                "dotted API paths through lists: memberList.tag, memberList.townHallLevel for family TH counts; "
                "heroes.name, heroes.level for heroes; clan.members.attacks for war attacks. "
                "Select parent fields to include their full subtree. detail=full includes everything; fields overrides it. "
                "Use separate clan reads in one plan for complete default rosters. "
                "Clash timestamps: 20261008T120000.000Z, UTC; battle before is exclusive.",
                "CWL: a season key is the league's month, YYYY-MM, or a key a read returned; "
                "read_cwl_results and read_war_attendance take and return it. "
                "family.cwl_seasons.season_key selects it, cwl_rounds.round_no (1-7) is the CWL day; "
                "join cwl_rounds.war_id to family.wars.id and season_id to cwl_seasons.id. "
                "For clans configured for seasonal cohort lookups, resolve the season's assigned roster or CWL participants before "
                "ranking players; current memberList and clan-filtered stored rosters may be parked accounts. "
                "Read player.warStars for cumulative stars after selecting the CWL cohort. "
                "Seasonal cohort lookup configuration: " + ", ".join(code for code, clan in CLANS.items() if clan.cwl_only) + ". "
                "These lookup flags do not establish operational CWL-only policy or home-clan eligibility; "
                "use the applicable sourced community policy for those decisions. "
                "For every member question about these clans, select seasonal CWL players. "
                "read_clash clan excludes their parked membership by default; member_scope=current "
                "is only for explicitly requested current in-game membership.",
                *([
                    "Recruitment: read_recruitment_pipeline returns active trials, trial-end "
                    "follow-ups and applicant reply reminders by ticket channel ID. "
                    "These are current workflow records, not application/conversion totals. "
                    f"Trial-list is channel {TRIAL_LIST}; saved ticket transcripts are in {TICKETS_LOG}. "
                    "Combine these records with trial-list posts, ticket acceptance messages and "
                    "transcripts by ticket/member ID before claiming placement or acceptance is missing. "
                    "Recruitment Support concerns advertisements, not application ticket state. "
                    "/accept admits the applicant and starts the trial. Trial-end follow-up asks "
                    "whether the member will stay and requests feedback; wait for their reply, "
                    "then /finalize and close with a transcript. It is not a new accept/decline stage. "
                    "No active trial or missing reminder does not prove a trial never happened."
                ] if "recruitment" in key else []),
                "Family clans:", self.clans, *entries,
                *([
                    "Not available to this asker: " + ", ".join(unavailable) + "."
                ] if unavailable else []),
            ])
        return self.variants[key]


def _column(row, family=False):
    name, kind = row[1], row[2]
    unit = (
        " (Unix us UTC)" if family and kind.upper() == "INTEGER" and name.endswith("_at")
        else " (Unix s UTC)" if name.endswith("_ts")
        else " (ISO UTC)" if kind.upper() == "TEXT" and name.endswith(("_utc", "_at")) else ""
    )
    kind = "INT" if kind.upper() == "INTEGER" else kind
    primary = f" PK{row[5]}" if row[5] else ""
    return f"{name} {kind}{primary}{unit}"
