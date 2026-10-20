"""Render narrow numeric lookups from typed tool fields, never generated numbers."""

import math
import re

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.configuration.clans import CLANS
from .grounding import GROUNDING_GAPS, GroundingError
from .strict_answers import strict_answer, strict_mode
from .answer_evidence import observations


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _clash_results(evidence, kind):
    # Only top-level tool results qualify: quoted messages and arbitrary nested
    # JSON must not be able to introduce trusted numeric findings.
    return [value for value in observations(evidence)
            if value.get("source") == "clash_api" and value.get("kind") == kind]


def narrow_answer(question, evidence):
    """Return None for open-ended or unsupported intents, fail closed for known lookups."""
    try:
        mode = strict_mode(question)
        if mode is not None:
            try:
                if mode == "count" and _count_intent(question.casefold()):
                    return _member_count(question.casefold(), _clash_results(evidence, "clan"))
                return strict_answer(question, evidence)
            except GroundingError as error:
                error.kind = "count" if mode == "count" else "list"
                raise
    except GroundingError as error:
        if error.kind == "source":
            error.kind = "list"
        raise
    question = question.casefold().strip()
    if (len(question.split()) > 45 or re.search(
            r"\b(why|recommend|should|advice|assess|evaluate|explain|promote|kick)\b", question)):
        return None
    count = _count_intent(question)
    requested = []
    if re.search(r"\b(average|avg|mean)\s+(?:defen[cs]e\s+|attack\s+)?stars\b", question):
        requested.append(("average_stars", "Average stars", ""))
    if re.search(r"\b(triple|three.star|3.star)\s+(rate|percentage)\b", question):
        requested.append(("triple_rate_percent", "Triple rate", "%"))
    if re.search(r"\b(average|avg|mean)\s+destruction\b", question):
        requested.append(("average_destruction_percent", "Average destruction", "%"))
    if not count and not requested:
        return None
    try:
        return _numeric_answer(question, evidence, count=count, requested=requested)
    except GroundingError as error:
        error.kind = "count" if count and not requested else "figures"
        raise


def _numeric_answer(question, evidence, *, count, requested):
    if count and requested:
        raise GroundingError("ambiguous_numeric_request")
    results = _clash_results(evidence, "clan" if count else "battle_log")
    if count:
        return _member_count(question, results)
    if len(results) != 1:
        raise GroundingError("numeric_source_missing_or_ambiguous")
    result = results[0]
    items = result.get("items", [])
    # Clan batches can be compacted into a column/row table.
    if isinstance(items, dict) and isinstance(items.get("columns"), list):
        items = [dict(zip(items["columns"], row)) for row in items.get("rows", [])]
    if not isinstance(items, list) or not items:
        raise GroundingError("numeric_source_missing")
    tags = {normalize_player_tag(value) for value in re.findall(r"#[a-z0-9]+", question)}
    if tags and (None in tags or not tags.issubset({item.get("tag") for item in items})):
        raise GroundingError("numeric_source_wrong_accounts")
    aggregate = result.get("aggregate")
    if not isinstance(aggregate, dict):
        raise GroundingError("battle_aggregate_missing")
    selection = aggregate.get("selection", "")
    if ("ranked" in question and not selection.startswith("ranked_")
            or re.search(r"\bdefen[cs]es?\b", question) and not selection.endswith("_defenses")
            or re.search(r"\battacks?\b", question) and not selection.endswith("_attacks")):
        raise GroundingError("battle_aggregate_wrong_selection")
    coverage = aggregate.get("coverage", {})
    lines = []
    incomplete = False
    for field, label, suffix in requested:
        metric = aggregate.get(field, {})
        numerator, denominator = (
            (metric.get("numerator"), metric.get("denominator"))
            if isinstance(metric, dict) else (None, None)
        )
        if not _number(numerator) or type(denominator) is not int or denominator < 0:
            incomplete = True
            continue
        if denominator == 0:
            lines.append(f"{label}: unavailable (no measured battles).")
            continue
        # Format the validated ratio itself; a model-provided value/display can
        # never replace the numerator and denominator computed by the tool.
        places = 4 if field == "average_stars" else 2
        value = f"{numerator / denominator:.{places}f}"
        lines.append(f"{label}: {value}{suffix} ({denominator} measured battles).")
    if not lines:
        raise GroundingError("battle_metric_invalid")
    if incomplete:
        lines.append(GROUNDING_GAPS["figures"])
    after, before = coverage.get("requested_after"), coverage.get("requested_before")
    if after or before:
        lines.append(f"Window: {after or 'start of available log'} to {before or 'latest available battle'} (end exclusive).")
    lines.append("Observed rolling logs only; full history is not established.")
    unavailable = coverage.get("unavailable_tags", [])
    if unavailable:
        lines.append(f"Unavailable accounts: {len(unavailable)}.")
    unknown_dates = sum(account.get("unknown_timestamp_rows", 0) for account in coverage.get("accounts", []))
    if unknown_dates:
        lines.append(f"Battles with unknown dates: {unknown_dates}.")
    missing = aggregate.get("missing_stars", 0) + aggregate.get("missing_destruction", 0)
    if missing:
        lines.append("Missing measurements are excluded from each metric's denominator.")
    return "\n".join(lines)


def _count_intent(question):
    return bool(re.search(r"\b(member count|count of (?:clan )?members|how many (?:clan )?members|members? count)\b", question))


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


def _member_count(question, results):
    """Resolve the subject before comparing successful observations.

    A failed attempt is not a competing observation. Repeated equal reads are
    harmless; different successful counts for the selected clan remain a conflict.
    """
    tags = _clan_subjects(question)
    items = []
    for result in results:
        batch = result.get("items", [])
        if isinstance(batch, dict) and isinstance(batch.get("columns"), list):
            batch = [dict(zip(batch["columns"], row)) for row in batch.get("rows", [])]
        items.extend(item for item in batch if isinstance(item, dict) and item.get("status") == "ok")
    if not tags:
        named = {item.get("tag") for item in items
                 if isinstance(name := item.get("data", {}).get("name"), str)
                 and name.strip() and re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", question)}
        if named:
            tags = named
        elif re.search(r"\bmembers?(?:\s+count)?\s+(?:in|for|of)\s+\S", question):
            raise GroundingError("member_count_wrong_subject")
    observations = set()
    for item in items:
        tag = normalize_player_tag(item.get("tag"))
        if tags and tag not in tags:
            continue
        value = item.get("data", {}).get("members")
        if tag is None or type(value) is not int or value < 0:
            raise GroundingError("member_count_invalid")
        observations.add((tag, value))
    if len(tags) > 1 or len(observations) != 1:
        raise GroundingError("member_count_missing_or_ambiguous")
    return str(next(iter(observations))[1])
