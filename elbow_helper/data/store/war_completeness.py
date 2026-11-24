"""Side evidence and whole-war completeness from stored facts, for every writer."""

import json
import sqlite3

from elbow_helper.domain.wars import score_result


def war_side_coverages(connection, war_ids):
    """Read side evidence for a set of wars in four statements."""
    ids = json.dumps(list(war_ids))
    wars = {row["id"]: row for row in connection.execute(
        "SELECT * FROM wars WHERE id IN (SELECT value FROM json_each(?))", (ids,))}
    members = {(row["war_id"], row["side"]): row for row in connection.execute(
        "SELECT war_id,side,COUNT(*) AS members,"
        "SUM(name IS NULL OR townhall IS NULL OR source_map_position IS NULL) AS missing_lineup,"
        "SUM(attacks_expected IS NULL OR attacks_used IS NULL) AS missing_counts "
        "FROM war_members WHERE war_id IN (SELECT value FROM json_each(?)) GROUP BY war_id,side", (ids,))}
    attacks = {}
    unattributed = {}
    # With neither attacker nor defender known, an attack could belong to either side.
    for row in connection.execute(
            "SELECT a.war_id,m.side,"
            "SUM(a.attacker_member_id IS NULL AND a.defender_member_id IS NULL) AS unattributed,"
            "SUM(COALESCE(json_extract(a.extra_json,'$.stub'),0)=0) AS stored "
            "FROM war_attacks a LEFT JOIN war_members m ON m.id=a.attacker_member_id "
            "WHERE a.war_id IN (SELECT value FROM json_each(?)) GROUP BY a.war_id,m.side", (ids,)):
        attacks[row["war_id"], row["side"]] = row["stored"]
        unattributed[row["war_id"]] = unattributed.get(row["war_id"], 0) + row["unattributed"]
    sides = {}
    for row in connection.execute(
            "SELECT * FROM war_sides WHERE war_id IN (SELECT value FROM json_each(?))", (ids,)):
        war = wars[row["war_id"]]
        count = members.get((war["id"], row["side"]))
        total = count["members"] if count else 0
        counted = war["team_size"] is not None and total == war["team_size"]
        # Bases copied from old attacks do not prove a captured opponent lineup.
        partial = war["type"] == "CWL" and json.loads(row["extra_json"]).get("partial", False)
        stored = attacks.get((war["id"], row["side"]), 0)
        sides.setdefault(war["id"], []).append({
            "clan_tag": row["clan_tag"], "team_size": war["team_size"], "members": total,
            "lineup_complete": counted and not (count["missing_lineup"] if count else 0) and not partial,
            "attacks_reported": row["attacks"], "attacks_stored": stored,
            "attacks_complete": (counted and not (count["missing_counts"] if count else 0)
                                 and not unattributed.get(war["id"], 0) and row["attacks"] is not None
                                 and stored == row["attacks"])})
    return {(war_id, own["clan_tag"]): {
        "war_id": war_id, "type": wars[war_id]["type"], "type_basis": wars[war_id]["type_basis"],
        "state": wars[war_id]["state"], "final": wars[war_id]["final_at"] is not None,
        "own": own, "opponent": next(side for side in pair if side is not own),
        "unattributed_attacks": unattributed.get(war_id, 0)}
        for war_id, pair in sides.items() for own in pair}


def war_side_coverage(connection, war_id, clan_tag):
    """Lineup and attack completeness of a clan's side, kept apart from its opponent's.

    Returns None when the clan did not fight this war.
    """
    return war_side_coverages(connection, [war_id]).get((war_id, clan_tag))



def upgrade_cwl_completeness(connection):
    """Version 16: old copies must not describe the family side as the whole war."""
    for war in connection.execute("SELECT w.id,s.clan_tag FROM wars w JOIN war_sides s ON s.war_id=w.id "
                                  "AND s.side=0 WHERE w.type='CWL' AND EXISTS (SELECT 1 FROM war_sides x "
                                  "WHERE x.war_id=w.id AND json_type(x.extra_json,'$.legacy')='object' "
                                  "AND json_extract(x.extra_json,'$.partial')=1)").fetchall():
        coverage = war_side_coverage(connection, war["id"], war["clan_tag"])
        roster = all(coverage[side]["lineup_complete"] for side in ("own", "opponent"))
        attacks = all(coverage[side]["attacks_complete"] for side in ("own", "opponent"))
        connection.execute("UPDATE wars SET roster_complete=?,attacks_complete=? WHERE id=?",
                           (int(roster), int(attacks), war["id"]))


def fill_blank_war_facts(connection):
    """Version 23: facts that follow from what is stored, filled where an old save left them blank.

    A final war with both scores gets the result its score gives, as a new save now does. Family
    clan names come from the clan list. The war log's league-week entry has no real opponent, so
    its opponent's level and destruction were saved as Clash's 0 and are now unknown.
    """
    named = connection.cursor()
    named.row_factory = sqlite3.Row
    for war_id, in named.execute(
            "SELECT id FROM wars WHERE state='warEnded' AND result_complete=1 AND EXISTS "
            "(SELECT 1 FROM war_sides WHERE war_id=wars.id AND result IS NULL)").fetchall():
        sides = named.execute("SELECT side,stars,destruction,result FROM war_sides WHERE war_id=?", (war_id,)).fetchall()
        if len(sides) != 2:
            continue
        for own, other in ((sides[0], sides[1]), (sides[1], sides[0])):
            result = score_result(own, other) if own["result"] is None else None
            if result is not None:
                connection.execute("UPDATE war_sides SET result=? WHERE war_id=? AND side=?", (result, war_id, own["side"]))
    for table, key in (("war_sides", "clan_tag"), ("cwl_clans", "clan_tag")):
        connection.execute(
            f"UPDATE {table} SET name=(SELECT c.name FROM clans c WHERE c.tag={table}.{key}) "
            f"WHERE (name IS NULL OR name='') AND EXISTS (SELECT 1 FROM clans c WHERE c.tag={table}.{key} "
            "AND c.name IS NOT NULL AND c.name<>'')")
    connection.execute("UPDATE cwl_log_summaries SET opponent_level=NULL WHERE opponent_level=0")
    connection.execute("UPDATE cwl_log_summaries SET opponent_destruction=NULL WHERE opponent_destruction=0")


def fill_cwl_allowance(connection):
    """Version 18: league wars saved before their allowance was, which is one attack per member.

    Clash lists a member's attacks, so a stored used count is Clash's own. A
    defender copied from old attacks has none: it stays unknown, never a zero or a miss.
    """
    connection.execute("UPDATE wars SET attacks_per_member=1 WHERE type='CWL' AND attacks_per_member IS NULL")
    connection.execute(
        "UPDATE war_members SET attacks_expected=(SELECT w.attacks_per_member FROM wars w WHERE w.id=war_id) "
        "WHERE attacks_expected IS NULL AND attacks_used IS NOT NULL "
        "AND war_id IN (SELECT id FROM wars WHERE type='CWL')")
