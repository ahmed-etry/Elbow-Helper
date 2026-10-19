"""Validate source references without treating English wording as proof or error."""

from collections.abc import Mapping
import re

import discord


MESSAGE = re.compile(r"\[\[message:(\d+)\]\]")
LINK = re.compile(r"https://discord(?:app)?\.com/channels/\d+/\d+/\d+")
GROUNDING_INSTRUCTION = (
    "Answer the question from the evidence. Separate facts from recommendations and qualify "
    "uncertain parts. [[message:ID]] optionally inserts an exact read message."
)
GROUNDING_GAPS = {
    "source": "I couldn’t confirm the quoted message.",
    "count": "I couldn’t confirm the requested count.",
    "list": "I couldn’t confirm the requested list.",
    "figures": "I couldn’t confirm the requested figures.",
}


class GroundingError(ValueError):
    """A concrete source reference or typed result cannot support the requested output."""

    def __init__(self, reason, *, kind="source"):
        super().__init__(reason)
        self.kind = kind


def records(value):
    """Walk ordinary results and the compact column/row model representation."""
    if isinstance(value, Mapping):
        flags = value.get("flags")
        if "error" in value or isinstance(flags, Mapping) and flags.get("status") in ("failed", "refused"):
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
    """Validate source bindings; interpretation belongs to evidence-aware generation."""
    messages = messages_from(evidence)

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

    if "[[message:" in MESSAGE.sub("", answer):
        raise GroundingError("invalid_message_binding")
    return MESSAGE.sub(render, answer)


def referenced_sources(answer, evidence, *, limit=3):
    """Retain usable evidence after a failed repair, without retaining unverified prose."""
    messages = messages_from(evidence)
    identities = [match[1] for match in MESSAGE.finditer(answer)]
    identities.extend(match.group().rsplit("/", 1)[-1] for match in LINK.finditer(answer))
    selected = list(dict.fromkeys(identity for identity in identities if identity in messages))[:limit]
    return "\n\n".join(ground_answer(f"[[message:{identity}]]", evidence) for identity in selected)


def partial_answer(answer, evidence, error):
    """Keep referenced source material when generated source repair cannot finish."""
    return "\n\n".join(filter(None, (
        referenced_sources(answer, evidence), GROUNDING_GAPS[error.kind],
    )))
