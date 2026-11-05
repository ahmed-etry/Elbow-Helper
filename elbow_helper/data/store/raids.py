"""Each member's capital raid weekend as typed facts, beside the raw raid payloads.

One row per clan, weekend start and player tag. A running weekend's lines follow
the payload; a line is never removed, so a member who left the clan, and a
finished weekend, keep their last values. Raw payloads stay the lossless copy.
"""

from elbow_helper.domain.player_tags import normalize_player_tag

from .api_fields import read_fields
from .projections import nonnegative_int


# Typed column -> the field Clash sends.
FIELDS = {"attacks": "attacks", "attack_limit": "attackLimit",
          "bonus_attack_limit": "bonusAttackLimit", "capital_loot": "capitalResourcesLooted"}
COLUMNS = tuple(FIELDS)


def member_lines(members):
    """{player tag: values} from a raid weekend's member list.

    A member without a usable tag, or whose tag appears twice, has no safe
    identity and is left to the raw payload. A count that is missing or not a
    whole number is saved as NULL.
    """
    if not isinstance(members, list):
        return {}
    lines, repeated = {}, set()
    for member in members:
        tag = normalize_player_tag(member.get("tag")) if isinstance(member, dict) else None
        if tag is None:
            continue
        if tag in lines:
            repeated.add(tag)
        lines[tag] = {column: member.get(field) if nonnegative_int(member.get(field)) else None
                      for column, field in FIELDS.items()}
    return {tag: values for tag, values in lines.items() if tag not in repeated}


def save_member_lines(connection, clan_tag, weekend_start, members, run, at):
    """Write a fetched weekend's lines; unchanged lines and absent members are left alone."""
    for tag, values in member_lines(members).items():
        account = connection.execute("SELECT id FROM accounts WHERE player_tag=?", (tag,)).fetchone()
        old = connection.execute(
            "SELECT * FROM raid_member_lines WHERE clan_tag=? AND weekend_start=? AND player_tag=?",
            (clan_tag, weekend_start, tag)).fetchone()
        if old is None:
            connection.execute(
                f"INSERT INTO raid_member_lines(clan_tag,weekend_start,player_tag,account_id,{','.join(COLUMNS)},"
                "observed_at,source_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (clan_tag, weekend_start, tag, account[0] if account else None,
                 *(values[column] for column in COLUMNS), at, run))
            continue
        if any(old[column] != values[column] for column in COLUMNS):
            connection.execute(
                f"UPDATE raid_member_lines SET {','.join(column + '=?' for column in COLUMNS)},observed_at=?,source_run_id=? "
                "WHERE clan_tag=? AND weekend_start=? AND player_tag=?",
                (*(values[column] for column in COLUMNS), at, run, clan_tag, weekend_start, tag))
        if account and old["account_id"] is None:
            connection.execute("UPDATE raid_member_lines SET account_id=? WHERE clan_tag=? AND weekend_start=? "
                               "AND player_tag=?", (account[0], clan_tag, weekend_start, tag))


def stored_members(connection, record_scope):
    """The member list of a saved raw weekend, rebuilt from its field history."""
    members = {}
    for path, value in read_fields(connection, "capital_raids", record_scope).items():
        # "/members/<row selector>/<field>"; the selector is the row's identity or its source index.
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
        if len(parts) == 3 and parts[0] == "members":
            members.setdefault(parts[1], {})[parts[2]] = value
    return list(members.values())


def create_raid_schema(connection):
    """Create the weekend lines and fill them from the raw weekends already saved."""
    connection.execute("""CREATE TABLE raid_member_lines (
        clan_tag TEXT NOT NULL, weekend_start INTEGER NOT NULL, player_tag TEXT NOT NULL,
        account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
        attacks INTEGER, attack_limit INTEGER, bonus_attack_limit INTEGER, capital_loot INTEGER,
        observed_at INTEGER NOT NULL, source_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY(clan_tag,weekend_start,player_tag)) WITHOUT ROWID""")
    connection.execute("CREATE INDEX raid_lines_account ON raid_member_lines(account_id,weekend_start)")
    connection.execute("CREATE INDEX raid_lines_player ON raid_member_lines(player_tag,weekend_start)")
    backfill_raid_lines(connection)


def backfill_raid_lines(connection):
    """Lines of every raw weekend, as of the last run that changed its payload."""
    weekends = connection.execute(
        "SELECT e.scope_id,e.source_at,e.record_scope,e.first_run_id,r.id AS record FROM family_api_entities e "
        "LEFT JOIN api_records r ON r.source_kind=e.source_kind AND r.scope_id=e.record_scope "
        "WHERE e.source_kind='capital_raids' ORDER BY e.scope_id,e.source_at").fetchall()
    for clan_tag, start, record_scope, first_run, record in weekends:
        if record is None or start is None:
            continue
        last = connection.execute("SELECT observed_at,source_run_id FROM api_field_changes WHERE record_id=? "
                                  "ORDER BY source_run_id DESC LIMIT 1", (record,)).fetchone()
        save_member_lines(connection, clan_tag, start, stored_members(connection, record_scope),
                          last[1] if last else first_run, last[0] if last else start)
