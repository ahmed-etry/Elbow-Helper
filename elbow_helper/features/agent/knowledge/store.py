"""Versioned, read-only approved community knowledge from managed Markdown."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import tomllib
from typing import Any


LOGGER = logging.getLogger(__name__)
MAX_KNOWLEDGE_FILES = 256
MAX_KNOWLEDGE_FILE_BYTES = 64 * 1024
MAX_KNOWLEDGE_BODY_CHARACTERS = 32_000
MAX_KNOWLEDGE_SECTIONS_PER_RESULT = 20
VISIBILITIES = frozenset({"public", "core", "lead", "lead_plus"})
STATUSES = frozenset({"draft", "approved", "retired"})
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_REQUIRED_FIELDS = frozenset({
    "key", "version", "title", "topics", "source_refs", "effective_from",
    "reviewed_at", "approved_by", "visibility", "status",
})
_OPTIONAL_FIELDS = frozenset({"expires_at", "conflicts_with"})


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    key: str
    version: int
    title: str
    topics: tuple[str, ...]
    source_refs: tuple[str, ...]
    effective_from: str
    reviewed_at: str
    approved_by: int
    visibility: str
    status: str
    expires_at: str | None
    conflicts_with: tuple[str, ...]
    body: str
    content_sha256: str

    def __post_init__(self) -> None:
        effective = _timestamp(self.effective_from)
        reviewed = _timestamp(self.reviewed_at)
        expires = _timestamp(self.expires_at) if self.expires_at is not None else None
        if (
            not _valid_key(self.key)
            or type(self.version) is not int or not 1 <= self.version <= 9999
            or not isinstance(self.title, str) or not self.title.strip()
            or self.title != self.title.strip() or len(self.title) > 120
            or not isinstance(self.topics, tuple) or len(self.topics) > 16
            or len(set(self.topics)) != len(self.topics)
            or any(not _valid_key(value) for value in self.topics)
            or not isinstance(self.source_refs, tuple) or not self.source_refs
            or len(self.source_refs) > 16
            or len(set(self.source_refs)) != len(self.source_refs)
            or any(not isinstance(value, str) or not value or len(value) > 300
                   for value in self.source_refs)
            or type(self.approved_by) is not int or self.approved_by <= 0
            or self.visibility not in VISIBILITIES or self.status not in STATUSES
            or not isinstance(self.conflicts_with, tuple)
            or len(self.conflicts_with) > 16
            or len(set(self.conflicts_with)) != len(self.conflicts_with)
            or any(not _valid_key(value) for value in self.conflicts_with)
            or self.key in self.conflicts_with
            or not isinstance(self.body, str) or not self.body
            or len(self.body) > MAX_KNOWLEDGE_BODY_CHARACTERS
            or effective.isoformat() != self.effective_from
            or reviewed.isoformat() != self.reviewed_at
            or expires is not None and (
                expires.isoformat() != self.expires_at or expires <= effective
            )
            or self.content_sha256 != _section_hash_values(
                key=self.key, version=self.version, title=self.title,
                topics=self.topics, source_refs=self.source_refs,
                effective_from=self.effective_from, reviewed_at=self.reviewed_at,
                approved_by=self.approved_by, visibility=self.visibility,
                status=self.status, expires_at=self.expires_at,
                conflicts_with=self.conflicts_with, body=self.body,
            )
        ):
            raise ValueError("Invalid approved knowledge section")

    @property
    def section_id(self) -> str:
        return f"{self.key}@v{self.version}"


@dataclass(frozen=True, slots=True)
class KnowledgeCatalog:
    observed_at: str
    active_sections: tuple[KnowledgeSection, ...]
    retired_keys: tuple[str, ...]
    expired_keys: tuple[str, ...]
    draft_count: int
    issue_count: int

    @property
    def valid(self) -> bool:
        return self.issue_count == 0

    def search(
        self, sections: Iterable[KnowledgeSection], *, query: str = "",
        topics: Sequence[str] = (), limit: int = 10,
    ) -> tuple[KnowledgeSection, ...]:
        if (
            not isinstance(query, str) or len(query) > 200
            or not isinstance(topics, (list, tuple)) or len(topics) > 8
            or any(not _valid_key(topic) for topic in topics)
            or type(limit) is not int
            or not 1 <= limit <= MAX_KNOWLEDGE_SECTIONS_PER_RESULT
        ):
            raise ValueError("Invalid approved-knowledge search")
        needle = query.strip().casefold()
        requested_topics = frozenset(topic.casefold() for topic in topics)
        matches = []
        for section in sections:
            section_topics = frozenset(topic.casefold() for topic in section.topics)
            if requested_topics and not requested_topics <= section_topics:
                continue
            score = _match_score(section, needle)
            if score is None:
                continue
            matches.append((score, section.key, -section.version, section))
        matches.sort(key=lambda item: item[:3])
        return tuple(item[3] for item in matches[:limit])

    def sections_are_current(
        self, sections: Sequence[KnowledgeSection],
    ) -> bool:
        if not self.valid:
            return False
        current = {
            section.section_id: section for section in self.active_sections
        }
        return all(current.get(section.section_id) == section for section in sections)

    def references_are_current(
        self, references: Sequence[tuple[str, str]],
    ) -> bool:
        if not self.valid:
            return False
        current = {
            section.section_id: section.content_sha256
            for section in self.active_sections
        }
        return all(current.get(section_id) == digest
                   for section_id, digest in references)


class KnowledgeStore:
    """Reload a strict managed directory so approved edits need no deployment."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self, *, now: datetime | None = None) -> KnowledgeCatalog:
        observed = now or datetime.now(timezone.utc)
        if not isinstance(observed, datetime):
            raise ValueError("Invalid knowledge observation time")
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        if not self.directory.exists():
            return KnowledgeCatalog(observed.isoformat(), (), (), (), 0, 0)
        if not self.directory.is_dir():
            LOGGER.error("Approved knowledge path is not a directory: %s", self.directory)
            return KnowledgeCatalog(observed.isoformat(), (), (), (), 0, 1)
        paths = sorted(self.directory.glob("*.md"), key=lambda path: path.name.casefold())
        if len(paths) > MAX_KNOWLEDGE_FILES:
            LOGGER.error("Approved knowledge directory exceeds its file bound")
            return KnowledgeCatalog(observed.isoformat(), (), (), (), 0, 1)
        sections = []
        issues = 0
        identities = set()
        for path in paths:
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Knowledge entries must be regular files")
                if path.stat().st_size > MAX_KNOWLEDGE_FILE_BYTES:
                    raise ValueError("Knowledge file exceeds its size bound")
                section = _parse_section(path.read_bytes())
                identity = (section.key, section.version)
                if identity in identities:
                    raise ValueError("Duplicate knowledge section version")
                identities.add(identity)
                sections.append(section)
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                issues += 1
                LOGGER.exception("Approved knowledge file is invalid: %s", path.name)
        active, retired, expired, drafts = _resolve_sections(sections, observed)
        return KnowledgeCatalog(
            observed.isoformat(), active, retired, expired, drafts, issues,
        )

    def sections_are_current(
        self, sections: Sequence[KnowledgeSection],
    ) -> bool:
        catalog = self.load()
        return catalog.sections_are_current(sections)


def _parse_section(raw: bytes) -> KnowledgeSection:
    text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("+++\n"):
        raise ValueError("Knowledge file requires TOML front matter")
    marker = text.find("\n+++\n", 4)
    if marker < 0:
        raise ValueError("Knowledge front matter is not closed")
    metadata = tomllib.loads(text[4:marker])
    fields = set(metadata)
    if (
        not _REQUIRED_FIELDS <= fields
        or not fields <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS
    ):
        raise ValueError("Knowledge metadata fields are invalid")
    body = text[marker + 5:].strip()
    key = metadata["key"]
    topics = _keys(metadata["topics"], maximum=16)
    conflicts = _keys(metadata.get("conflicts_with", ()), maximum=16)
    refs = _strings(metadata["source_refs"], maximum=16, item_maximum=300)
    effective = _timestamp(metadata["effective_from"])
    reviewed = _timestamp(metadata["reviewed_at"])
    expires = (
        _timestamp(metadata["expires_at"])
        if metadata.get("expires_at") is not None else None
    )
    if (
        not _valid_key(key)
        or type(metadata["version"]) is not int
        or not 1 <= metadata["version"] <= 9999
        or not isinstance(metadata["title"], str) or not metadata["title"].strip()
        or len(metadata["title"]) > 120
        or type(metadata["approved_by"]) is not int
        or metadata["approved_by"] <= 0
        or metadata["visibility"] not in VISIBILITIES
        or metadata["status"] not in STATUSES
        or not refs
        or not body or len(body) > MAX_KNOWLEDGE_BODY_CHARACTERS
        or expires is not None and expires <= effective
        or key in conflicts
    ):
        raise ValueError("Knowledge section values are invalid")
    values = {
        "key": key, "version": metadata["version"],
        "title": metadata["title"].strip(), "topics": topics,
        "source_refs": refs, "effective_from": effective.isoformat(),
        "reviewed_at": reviewed.isoformat(),
        "approved_by": metadata["approved_by"],
        "visibility": metadata["visibility"], "status": metadata["status"],
        "expires_at": expires.isoformat() if expires is not None else None,
        "conflicts_with": conflicts, "body": body,
    }
    return KnowledgeSection(
        **values, content_sha256=_section_hash_values(**values),
    )


def _resolve_sections(
    sections: Sequence[KnowledgeSection], now: datetime,
) -> tuple[tuple[KnowledgeSection, ...], tuple[str, ...], tuple[str, ...], int]:
    grouped: dict[str, list[KnowledgeSection]] = {}
    drafts = 0
    for section in sections:
        if section.status == "draft":
            drafts += 1
            continue
        grouped.setdefault(section.key, []).append(section)
    active = []
    retired = []
    expired = []
    for key, versions in grouped.items():
        effective = [
            section for section in versions
            if datetime.fromisoformat(section.effective_from) <= now
        ]
        if not effective:
            continue
        latest = max(effective, key=lambda section: section.version)
        if latest.status == "retired":
            retired.append(key)
        elif (
            latest.expires_at is not None
            and datetime.fromisoformat(latest.expires_at) <= now
        ):
            expired.append(key)
        else:
            active.append(latest)
    active.sort(key=lambda section: (section.key, section.version))
    return tuple(active), tuple(sorted(retired)), tuple(sorted(expired)), drafts


def _match_score(section: KnowledgeSection, needle: str) -> int | None:
    if not needle:
        return 3
    if needle == section.key.casefold() or needle in {
        topic.casefold() for topic in section.topics
    }:
        return 0
    if needle in section.title.casefold():
        return 1
    if needle in section.body.casefold() or any(
        needle in topic.casefold() for topic in section.topics
    ):
        return 2
    tokens = tuple(token for token in re.split(r"\W+", needle) if token)
    haystack = " ".join((
        section.key, section.title, *section.topics, section.body,
    )).casefold()
    return 4 if tokens and all(token in haystack for token in tokens) else None


def _keys(value: Any, *, maximum: int) -> tuple[str, ...]:
    values = _strings(value, maximum=maximum, item_maximum=80)
    if any(not _valid_key(item) for item in values):
        raise ValueError("Knowledge keys are invalid")
    return values


def _strings(
    value: Any, *, maximum: int, item_maximum: int,
) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple)) or len(value) > maximum
        or any(not isinstance(item, str) or not item or len(item) > item_maximum
               for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("Knowledge metadata list is invalid")
    return tuple(value)


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError("Knowledge timestamp is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _section_hash_values(**values: Any) -> str:
    return hashlib.sha256(json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


__all__ = [
    "KnowledgeCatalog", "KnowledgeSection", "KnowledgeStore",
    "MAX_KNOWLEDGE_SECTIONS_PER_RESULT", "STATUSES", "VISIBILITIES",
]
