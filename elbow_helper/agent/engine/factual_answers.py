"""Check numeric claims against typed tool fields without replacing prose."""

import math
import re

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.configuration.clans import CLANS
from .grounding import GroundingError
from .strict_answers import strict_answer
from .answer_evidence import observations


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _clash_results(evidence, kind):
    # Only top-level tool results qualify: quoted messages and arbitrary nested
    # JSON must not be able to introduce trusted numeric findings.
    return [value for value in observations(evidence)
            if value.get("source") == "clash_api" and value.get("kind") == kind]


def _clan_subjects(question):
    tags = {normalize_player_tag(tag) for tag in re.findall(r"#[a-z0-9]+", question)}
    tags.update(clan.tag for code, clan in CLANS.items() if any(
        re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", question)
        for name in (code, clan.name)
    ))
    return tags


def clan_count_tag(question):
    """Resolve an explicit single-clan count without a model inventing its tag."""
    question = question.casefold().strip()
    subjects = "|".join(re.escape(value.casefold()) for code, clan in CLANS.items()
                        for value in (code, clan.name))
    subject = rf"(?:{subjects}|#[a-z0-9]+)"
    if not re.fullmatch(
        r"(?:<@!?\d+>\s*|@[\w]+(?: [\w]+){0,3}\s+)?(?:please\s+)?"
        rf"(?:how many (?:clan )?members (?:are (?:there )?)?in {subject}"
        rf"|(?:what(?:'s| is) (?:the )?)?(?:member count|count of members) (?:in|for|of) {subject})"
        r"(?:\s+(?:currently|right now|now|today))?(?:[ ,]+(?:number|count)[ -]only)?[?!. ]*",
        question,
    ):
        return None
    tags = _clan_subjects(question)
    return next(iter(tags)) if len(tags) == 1 and None not in tags else None


def _items(result):
    items = result.get("items", [])
    if isinstance(items, dict):
        columns, rows = items.get("columns"), items.get("rows")
        if (not isinstance(columns, list) or len(set(columns)) != len(columns)
                or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
            return []
        items = [dict(zip(columns, row)) for row in rows]
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _mentions(text, aliases):
    return any(re.search(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", text)
               for alias in aliases if alias)


def _different_number(claim, value, *, count=False):
    # An average rounded to the precision used in prose does not contradict
    # the tool's ratio. Counts remain exact.
    places = len(claim.partition(".")[2])
    tolerance = 0 if count else 0.5 * 10 ** -places + 1e-9
    return abs(float(claim) - value) > tolerance


_FILTERED_MEMBERS = re.compile(
    r"\b(?:th\s*\d+|town\s*hall|missed|attacks?|co.?leaders?|elders?|active|inactive|"
    r"eligible|participat\w*|donat\w*|who|that|with|without)\b"
)
_MEMBER_CLAIMS = re.compile(
    r"(?P<before>\d+(?:\.\d+)?)\s+(?:(?:total|clan|current)\s+)?members?\b"
    r"|\b(?:member(?:ship)? count|total(?: number)? of members|members)"
    r"\s*(?:is|are|:|=|of|at|stands at)?\s*(?P<after>\d+(?:\.\d+)?)\b"
)
_METRICS = (
    ("average_stars", "Average stars", r"(?:average|avg|mean)\s+(?:(?:defen[cs]e|attack)\s+)?stars", "", 4),
    ("triple_rate_percent", "Triple rate", r"(?:triple|three.star|3.star)\s+(?:rate|percentage)", "%", 2),
    ("average_destruction_percent", "Average destruction", r"(?:average|avg|mean)\s+destruction", "%", 2),
)


def numeric_conflicts(question, answer, evidence):
    """Return verified figures only for contradictory claims of the same metric.

    This deliberately checks explicit numeric claims, not the completeness or
    truth of arbitrary prose. Missing, ambiguous and failed data are not grounds
    for rewriting an answer. Quoted messages cannot supply trusted figures.
    """
    question = question.casefold()
    # Keep decimal points and account tags intact when separating claims.
    clauses = re.split(r"[\n;!?]+|(?<!\d)\.(?:\s|$)|(?<=\d)\.(?=\s|$)", answer.casefold())
    counts = {}
    for result in _clash_results(evidence, "clan"):
        for item in _items(result):
            data = item.get("data", {})
            tag = normalize_player_tag(item.get("tag"))
            if (item.get("status") != "ok" or tag is None or not isinstance(data, dict)
                    or type(data.get("members")) is not int or data["members"] < 0):
                continue
            values, aliases = counts.setdefault(tag, (set(), {tag}))
            values.add(data["members"])
            if isinstance(data.get("name"), str):
                aliases.add(data["name"])
            for code, clan in CLANS.items():
                if clan.tag == tag:
                    aliases.update((code, clan.name))
    conflicts = []
    for tag, (values, aliases) in counts.items():
        if len(values) != 1:
            continue
        value = next(iter(values))
        request_subject = (clan_count_tag(question) == tag
                           or question.strip(" ?.!\n") in ("member count", "count only", "just the count")
                           and len(counts) == 1)
        other_aliases = {alias for other, (_, names) in counts.items() if other != tag for alias in names}
        for clause in clauses:
            if _FILTERED_MEMBERS.search(clause):
                continue
            if _FILTERED_MEMBERS.search(question) and not re.search(r"\b(?:total|member(?:ship)? count)\b", clause):
                continue
            clause_tags = {normalize_player_tag(value) for value in re.findall(r"#[a-z0-9]+", clause)}
            if clause_tags and tag not in clause_tags:
                continue
            explicit = _mentions(clause, aliases)
            if not explicit and (not request_subject or _mentions(clause, other_aliases)):
                continue
            claims = [match["before"] or match["after"] for match in _MEMBER_CLAIMS.finditer(clause)]
            bare = re.match(r"^\s*(\d+(?:\.\d+)?)(?=\s*(?:[,:.]|$))", clause)
            if request_subject and bare:
                claims.append(bare[1])
            if any(_different_number(claim, value, count=True) for claim in claims):
                conflicts.append(f"{tag} total members: {value}.")
                break
    if re.fullmatch(r"\s*(?:count[ -]only|(?:only|just) (?:the )?count)[?!. ]*", question):
        query_counts = {str(index): result for index, result in enumerate(observations(evidence))
                        if "rows" in result and "row_count" in result}
        try:
            expected = strict_answer(question, query_counts)
        except GroundingError:
            expected = None
        if expected is not None and expected.isdigit():
            claim = re.fullmatch(r"\s*(?:count\s*[:=]?\s*)?(\d+)\s*[.!]?\s*", answer, re.I)
            if claim and int(claim[1]) != int(expected):
                conflicts.append(f"Requested count: {expected}.")
    results = _clash_results(evidence, "battle_log")
    # Multiple aggregates may cover different windows or cohorts. Do not guess
    # which one supports a generated figure.
    if len(results) == 1:
        result = results[0]
        aggregate = result.get("aggregate", {})
        items = _items(result)
        tags = {item.get("tag") for item in items if item.get("status") == "ok"}
        requested_tags = _clan_subjects(question)
        if (isinstance(aggregate, dict) and tags and None not in tags
                and (not requested_tags or requested_tags == tags)):
            selection = aggregate.get("selection", "")
            if (re.search(r"\battacks?\b", question) and not selection.endswith("_attacks")
                    or re.search(r"\bdefen[cs]es?\b", question) and not selection.endswith("_defenses")
                    or "ranked" in question and not selection.startswith("ranked_")):
                return tuple(dict.fromkeys(conflicts))
            requested_metrics = [field for field, _, pattern, _, _ in _METRICS if re.search(pattern, question)]
            for field, label, pattern, suffix, places in _METRICS:
                metric = aggregate.get(field, {})
                if not isinstance(metric, dict):
                    continue
                numerator, denominator = metric.get("numerator"), metric.get("denominator")
                if not _number(numerator) or type(denominator) is not int or denominator <= 0:
                    continue
                value = numerator / denominator
                for clause in clauses:
                    mentioned_tags = {normalize_player_tag(tag) for tag in re.findall(r"#[a-z0-9]+", clause)}
                    if mentioned_tags and mentioned_tags != tags:
                        continue
                    if (re.search(r"\battacks?\b", clause) and not selection.endswith("_attacks")
                            or re.search(r"\bdefen[cs]es?\b", clause) and not selection.endswith("_defenses")
                            or "ranked" in clause and not selection.startswith("ranked_")):
                        continue
                    claims = re.findall(pattern + r"\s*(?:is|was|:|=|of)?\s*(\d+(?:\.\d+)?)", clause)
                    claims += re.findall(r"(\d+(?:\.\d+)?)%?\s+" + pattern, clause)
                    bare = re.fullmatch(r"\s*(\d+(?:\.\d+)?)%?\s*", clause)
                    if requested_metrics == [field] and bare:
                        claims.append(bare[1])
                    if any(_different_number(claim, value) for claim in claims):
                        subjects = ", ".join(sorted(tags))
                        conflicts.append(f"{subjects} {selection} {label.lower()}: {value:.{places}f}{suffix} "
                                         f"({denominator} measured battles; observed rolling logs only).")
                        break
    return tuple(dict.fromkeys(conflicts))
