"""Check explicit current in-game rank claims against typed Clash observations."""

import re

from elbow_helper.domain.player_tags import normalize_player_tag
from .answer_evidence import observations


ROLES = {"member": "member", "admin": "elder", "elder": "elder",
         "coleader": "co-leader", "leader": "leader"}
ROLE = re.compile(r"(?<!\w)(co[ -]?leaders?|leaders?|elders?|members?)(?!\w)", re.I)


def _rows(value):
    if isinstance(value, dict) and isinstance(value.get("columns"), list):
        columns = value["columns"]
        if (not all(isinstance(column, str) for column in columns)
                or len(set(columns)) != len(columns) or not isinstance(value.get("rows"), list)):
            return []
        return [dict(zip(columns, row)) for row in value.get("rows", [])
                if isinstance(row, list) and len(row) == len(columns)]
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def role_conflicts(answer, evidence):
    """Only unambiguous, present-tense claims about one observed account qualify.

    Discord roles, quoted history, recommendations and conflicting observations
    cannot supply or contradict an in-game rank.
    """
    accounts = {}
    for result in observations(evidence):
        if result.get("source") != "clash_api" or result.get("kind") not in ("player", "clan"):
            continue
        for item in _rows(result.get("items")):
            if item.get("status") != "ok" or not isinstance(item.get("data"), dict):
                continue
            data = item["data"]
            rows = [{**data, "tag": item.get("tag")}] if result["kind"] == "player" else _rows(data.get("memberList"))
            for row in rows:
                tag = normalize_player_tag(row.get("tag"))
                role = ROLES.get(str(row.get("role", "")).casefold())
                if tag is None or role is None:
                    continue
                roles, aliases = accounts.setdefault(tag, (set(), {tag.casefold()}))
                roles.add(role)
                if isinstance(row.get("name"), str) and row["name"].strip():
                    aliases.add(row["name"].casefold())
    conflicts = []
    for clause in re.split(r"[\n;!?]+|\.(?:\s|$)", answer.casefold()):
        clause = re.sub(r"[*_`]+", "", clause)
        if (clause.lstrip().startswith(">") or re.search(
                r"\b(discord|server|should|could|would|might|if|promot\w*|demot\w*|"
                r"formerly|previously|was|were|not|isn't|isn’t|no longer)\b", clause)):
            continue
        explicit_tags = {normalize_player_tag(tag) for tag in re.findall(r"#[a-z0-9]+", clause)}
        subjects = [tag for tag, (_, aliases) in accounts.items()
                    if tag in explicit_tags or not explicit_tags and any(
                        re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", clause) for alias in aliases)]
        if len(subjects) != 1:
            continue
        tag = subjects[0]
        roles, aliases = accounts[tag]
        name_spans = [match.span() for alias in aliases for match in re.finditer(
            r"(?<!\w)" + re.escape(alias) + r"(?!\w)", clause)]
        claims = {ROLES.get(re.sub(r"[ -]", "", match[0]).rstrip("s")) for match in ROLE.finditer(clause)
                  if not any(start <= match.start() and match.end() <= end for start, end in name_spans)}
        if len(claims) != 1:
            continue
        if len(roles) == 1 and claims != roles:
            conflicts.append(f"{tag} in-game role: {next(iter(roles))}.")
    return tuple(dict.fromkeys(conflicts))
