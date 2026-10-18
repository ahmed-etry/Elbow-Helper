"""Bind personal attributions to messages supplied during this request.

Free prose is deliberately not accepted as proof that a person holds a stance.
The message form renders the source's words and author, without paraphrasing.
The lexical guard is conservative English detection, not a semantic entailment
model; behavioural evaluation remains necessary for unrestricted judgement.
"""

from collections.abc import Mapping
import re

import discord


MESSAGE = re.compile(r"\[\[message:(\d+)\]\]")
ATTRIBUTION = re.compile(
    r"\b(said|says|saying|told|stated|stating|wrote|writes|mentioned|mentions|"
    r"objected|objects|opposed|opposes|opposition|objection|complained|complains|"
    r"argued|argues|insisted|insists|requested|requests|asked|asks|"
    r"wants|wanted|prefers|preferred|believes|believed|thinks|thought|"
    r"feels|felt|supports|supported|endorsed|agreed|agrees|disagreed|disagrees|"
    r"vetoed|vetoes|rejected|rejects|demanded|demands|favours|favors|"
    r"concerned|concerns|stance|against|in favor|according to|"
    r"[’']s (?:view|opinion|preference|position)|in .{1,50}'s (?:view|opinion))\b",
    re.IGNORECASE,
)
REQUEST_OR_SELF = re.compile(
    r"^(?:I (?:said|thought|asked)|you (?:asked|requested))\b", re.IGNORECASE,
)
LINK = re.compile(r"https://discord(?:app)?\.com/channels/\d+/\d+/\d+")

GROUNDING_INSTRUCTION = (
    "Use [[message:ID]] for personal attributions: renders author, exact text and link "
    "from messages read this turn. No stance paraphrases; keep recommendations separate."
)
GROUNDING_UNAVAILABLE = "I couldn't verify that answer from the sources I read."


class GroundingError(ValueError):
    """An answer has unsupported personal attributions or invalid source bindings."""


def records(value):
    """Walk ordinary results and the compact column/row model representation."""
    if isinstance(value, Mapping):
        if value.get("error") or value.get("flags", {}).get("status") in ("failed", "refused"):
            return
        columns, rows = value.get("columns"), value.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            for row in rows:
                if isinstance(row, list) and len(row) == len(columns):
                    yield from records(dict(zip(columns, row)))
        yield value
        for key, item in value.items():
            if key not in ("columns", "rows") or not isinstance(columns, list):
                yield from records(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from records(item)


def messages_from(evidence):
    messages = {}
    conflicts = set()
    for row in records(evidence):
        if not all(key in row for key in ("message_id", "author_id", "content", "source")):
            continue
        identity = str(row["message_id"])
        source = row["source"]
        if (not identity.isdigit() or not isinstance(source, str) or not LINK.fullmatch(source)
                or source.rsplit("/", 1)[-1] != identity or not isinstance(row["content"], str)
                or not str(row["author_id"]).isdigit()):
            continue
        if identity in messages and any(messages[identity].get(field) != row.get(field)
                                        for field in ("author_id", "author", "content", "source")):
            conflicts.add(identity)
        messages[identity] = row
    return {key: value for key, value in messages.items() if key not in conflicts}


def ground_answer(answer, evidence):
    _check_field_meanings(answer, evidence)
    messages = messages_from(evidence)
    # Check the surrounding prose before inserting source text. A valid citation
    # beside an invented objection is still an unsupported attribution.
    prose = MESSAGE.sub("", answer)
    prose = re.sub(r"https?://\S+", "", prose)
    # This idiom contains no proposition about what somebody actually said.
    prose = re.sub(r"\b(he|she|they) said what \1 said\b", "", prose, flags=re.IGNORECASE)
    if re.search(r"(?:<@!?\d+>|\b[A-Z][\w ]{0,60}):\s*[\"“]", prose):
        raise GroundingError("personal_quote_requires_message")
    for sentence in re.split(r"[.!?\n]+", prose):
        sentence = sentence.strip(" *_->\t")
        if ATTRIBUTION.search(REQUEST_OR_SELF.sub("", sentence)):
            raise GroundingError("personal_attribution_requires_message")

    def render(match):
        row = messages.get(match[1])
        if row is None:
            raise GroundingError("message_not_read_this_turn")
        # Escape source Markdown and mentions so source words cannot masquerade
        # as the agent's own prose or create new notification targets.
        author = discord.utils.escape_markdown(str(row.get("author") or row["author_id"]))
        author = discord.utils.escape_mentions(author).replace("\n", " ")
        content = discord.utils.escape_mentions(discord.utils.escape_markdown(row["content"]))
        quoted = "\n".join("> " + line for line in content.splitlines())
        return f"{author} ([message]({row['source']})):\n{quoted}"

    result = MESSAGE.sub(render, answer)
    if "[[message:" in result:
        raise GroundingError("invalid_message_binding")
    return result


def _check_field_meanings(answer, evidence):
    """Reject the known unsupported profile/roster implications in factual prose."""
    kinds = {row.get("kind") for row in records(evidence) if row.get("source") == "clash_api"}
    for sentence in re.split(r"[.!?\n]+", MESSAGE.sub("", answer)):
        # Explicitly denying that the fields establish a fact is useful and safe.
        if re.search(r"\b(?:does not|doesn't|cannot|can't|do not|don't) (?:prove|establish|show|mean|imply)\b",
                     sentence, re.IGNORECASE):
            continue
        if "player" in kinds and not kinds.intersection({"current_war", "cwl_war", "war_log", "battle_log"}):
            if re.search(r"\b(inactive|barely active|no war (?:participation|attacks)|never attacks)\b",
                         sentence, re.IGNORECASE):
                raise GroundingError("profile_does_not_establish_activity")
        if "clan" in kinds:
            if re.search(r"\b(hard cap|at capacity|no more (?:members )?can join)\b", sentence, re.IGNORECASE):
                raise GroundingError("member_count_does_not_establish_capacity")
            if re.search(r"\b(newly (?:arrived|joined)|recently joined|newest (?:member|recruit)|join time)\b",
                         sentence, re.IGNORECASE):
                raise GroundingError("roster_rank_does_not_establish_join_time")
