"""Side evidence and whole-war completeness from stored facts, for every writer."""

import json


def war_side_coverage(connection, war_id, clan_tag):
    """Lineup and attack completeness of a clan's side, kept apart from its opponent's.

    Returns None when the clan did not fight this war.
    """
    war = connection.execute("SELECT * FROM wars WHERE id=?", (war_id,)).fetchone()
    sides = {row["clan_tag"]: row for row in connection.execute("SELECT * FROM war_sides WHERE war_id=?", (war_id,))}
    if war is None or clan_tag not in sides:
        return None
    # A known defender places an attack on the other side; with neither known,
    # the attack could be either side's.
    unattributed = connection.execute(
        "SELECT COUNT(*) FROM war_attacks WHERE war_id=? AND attacker_member_id IS NULL AND defender_member_id IS NULL",
        (war_id,)).fetchone()[0]

    def side(row):
        members = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(name IS NULL OR townhall IS NULL OR source_map_position IS NULL),0),"
            "COALESCE(SUM(attacks_expected IS NULL OR attacks_used IS NULL),0) "
            "FROM war_members WHERE war_id=? AND side=?", (war_id, row["side"])).fetchone()
        stored = connection.execute(
            "SELECT COUNT(*) FROM war_attacks a JOIN war_members m ON m.id=a.attacker_member_id "
            "WHERE a.war_id=? AND m.side=? AND COALESCE(json_extract(a.extra_json,'$.stub'),0)=0",
            (war_id, row["side"])).fetchone()[0]
        counted = war["team_size"] is not None and members[0] == war["team_size"]
        # Imported league defenders were observed only as bases that were hit.
        # Even when every base was hit, that is not a captured opponent lineup.
        partial = war["type"] == "CWL" and json.loads(row["extra_json"]).get("partial", False)
        lineup = counted and not members[1] and not partial
        # Every attack, and so every miss, needs each member's expected and used attacks, not their town hall
        # or map position.
        attacks = (counted and not members[2] and not unattributed and row["attacks"] is not None
                   and stored == row["attacks"])
        return {"clan_tag": row["clan_tag"], "team_size": war["team_size"], "members": members[0],
                "lineup_complete": lineup, "attacks_reported": row["attacks"], "attacks_stored": stored,
                "attacks_complete": attacks}

    other = next(row for tag, row in sides.items() if tag != clan_tag)
    return {"war_id": war_id, "type": war["type"], "type_basis": war["type_basis"], "state": war["state"],
            "final": war["final_at"] is not None, "own": side(sides[clan_tag]), "opponent": side(other),
            "unattributed_attacks": unattributed}



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
