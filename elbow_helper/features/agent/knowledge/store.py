"""Approved Markdown sections, reloaded when files change."""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import time
import tomllib


LOGGER = logging.getLogger(__name__)
VISIBILITIES = frozenset({"public", "lead", "lead_plus", "core"})
STATUSES = frozenset({"approved", "draft", "retired"})


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    title: str
    body: str
    visibility: str = "public"


@dataclass(frozen=True, slots=True)
class KnowledgeCatalog:
    observed_at: str
    active_sections: tuple[KnowledgeSection, ...]

    def search(self, sections, *, query=""):
        words = tuple(set(re.findall(r"\w+", query.casefold())))
        ranked = []
        for index, section in enumerate(sections):
            title = sum(word in section.title.casefold() for word in words)
            body = sum(word in section.body.casefold() for word in words)
            if not words or title or body:
                ranked.append((-title, -body, index, section))
        return tuple(item[3] for item in sorted(ranked)[:8])

    def public_prompt(self):
        from ..conversation.context import estimate_tokens
        text = "\n\n".join(f"### {section.title}\n{section.body}"
                             for section in self.active_sections if section.visibility == "public")
        return text if estimate_tokens(text) <= 4000 else ""


class KnowledgeStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._checked_at = float("-inf")
        self._signature = None
        self._catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), ())

    def load(self):
        now = time.monotonic()
        if now - self._checked_at < 60:
            return self._catalog
        self._checked_at = now
        try:
            paths = sorted(self.directory.glob("*.md"), key=lambda path: path.name.casefold())
            signature = tuple(
                (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths
            )
        except OSError:
            LOGGER.exception("Cannot inspect community knowledge files")
            return self._catalog
        if signature == self._signature:
            return self._catalog
        sections = []
        for path in paths:
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise ValueError("Knowledge file exceeds its size bound or is a symlink")
                sections.extend(_parse(path))
            except (OSError, UnicodeError, ValueError):
                LOGGER.exception("Cannot read community knowledge: %s", path.name)
        self._signature = signature
        self._catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), tuple(sections))
        return self._catalog


def _parse(path):
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    metadata = {}
    if text.startswith("+++\n"):
        marker = re.search(r"^\+\+\+\s*$", text[4:], re.MULTILINE)
        if marker is None:
            raise ValueError("Knowledge front matter is not closed")
        metadata = tomllib.loads(text[4:4 + marker.start()])
        text = text[4 + marker.end():].lstrip("\n")
    visibility = metadata.get("visibility", "public")
    status = metadata.get("status", "approved")
    if visibility not in VISIBILITIES or status not in STATUSES:
        raise ValueError("Invalid knowledge visibility or status")
    if status != "approved":
        return ()
    sections = []
    title = path.stem
    body = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        heading = re.match(r"^#{1,3}\s+(.+?)\s*#*\s*$", line) if not fenced else None
        if heading:
            if body or title != path.stem:
                sections.append(KnowledgeSection(title, "\n".join(body).strip(), visibility))
            title, body = heading[1], []
        else:
            body.append(line)
    if body or title != path.stem:
        sections.append(KnowledgeSection(title, "\n".join(body).strip(), visibility))
    return tuple(section for section in sections if section.body or section.title != path.stem)
