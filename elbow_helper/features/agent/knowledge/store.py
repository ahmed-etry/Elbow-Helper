"""Approved Markdown sections, reloaded when files change."""

from dataclasses import dataclass, field, replace
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import combinations
import logging
from pathlib import Path
import re
import time
import tomllib
from types import MappingProxyType

from ..conversation.context import estimate_tokens


LOGGER = logging.getLogger(__name__)
VISIBILITIES = frozenset({"public", "lead", "lead_plus", "core"})
STATUSES = frozenset({"approved", "draft", "retired"})
TOPIC_INDEX_TOKEN_LIMIT = 14_000


def _words(text):
    """Normalize whole words and regular English plurals without dependencies."""
    words = []
    for word in re.findall(r"\w+", text.casefold()):
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes", "zes")):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        words.append(word)
    return tuple(words)


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    title: str
    body: str
    visibility: str = "public"
    file_name: str = ""
    always_loaded: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgePrompt:
    core: str
    required_access: frozenset[str]
    core_tokens: int
    omitted: tuple[str, ...] = ()
    index: str = ""
    index_tokens: int = 0
    index_title_tokens: int = 0
    index_fallback: bool = False
    index_omitted: tuple[str, ...] = ()

    @property
    def text(self):
        return "\n\n".join(part for part in (self.core, self.index) if part)


@dataclass(frozen=True, slots=True)
class KnowledgeCatalog:
    observed_at: str
    active_sections: tuple[KnowledgeSection, ...]
    _prompts: Mapping[frozenset[str], KnowledgePrompt] = field(default_factory=dict, repr=False,
                                                            compare=False)

    def search(self, sections, *, query=""):
        phrase = _words(query)
        words = set(phrase)
        ranked = []
        for index, section in enumerate(sections):
            title_words = _words(section.title)
            title, body = Counter(title_words), Counter(_words(section.body))
            coverage = sum(word in title or word in body for word in words)
            score = sum(3 * title[word] + body[word] for word in words)
            if not words or coverage:
                exact = bool(phrase) and phrase == title_words
                ranked.append((-exact, -coverage, -score, index, section))
        return tuple(item[-1] for item in sorted(ranked)[:8])

    def visible_sections(self, levels=()):
        return tuple(section for section in self.active_sections
                     if section.visibility == "public" or section.visibility in levels)

    def prompt(self, levels=()):
        key = frozenset(levels) & (VISIBILITIES - {"public"})
        if key in self._prompts:
            return self._prompts[key]
        return self._build_prompt(key)

    def _build_prompt(self, levels):
        included, omitted, access = [], [], set()
        visible = self.visible_sections(levels)
        for section in visible:
            if not section.always_loaded:
                continue
            text = f"### {section.title}\n{section.body}"
            if estimate_tokens("\n\n".join((*included, text))) > 4000:
                omitted.append(f"{section.file_name}: {section.title}")
                continue
            included.append(text)
            if section.visibility != "public":
                access.add(section.visibility)
        core = "\n\n".join(included)
        index, title_tokens, fallback, index_omitted = _topic_index(visible)
        if index:
            access.update(section.visibility for section in visible if section.visibility != "public")
        return KnowledgePrompt(core, frozenset(access), estimate_tokens(core), tuple(omitted),
                               index, estimate_tokens(index), title_tokens, fallback, index_omitted)

    def prepare_prompts(self):
        levels = sorted(VISIBILITIES - {"public"})
        prompts = {frozenset(profile): self._build_prompt(profile)
                   for size in range(len(levels) + 1) for profile in combinations(levels, size)}
        return replace(self, _prompts=MappingProxyType(prompts))

    def log_reload(self, counts):
        """Report each reload once, including every supported visibility profile."""
        estimates, omitted, fallback, index_omitted = {}, set(), [], set()
        for levels, prompt in self._prompts.items():
            profile = ",".join(("public", *sorted(levels)))
            estimates[profile] = {"core_tokens": prompt.core_tokens, "index_tokens": prompt.index_tokens,
                                  "index_title_tokens": prompt.index_title_tokens}
            omitted.update(prompt.omitted)
            index_omitted.update(prompt.index_omitted)
            if prompt.index_fallback:
                fallback.append(profile)
        # The broadest profile is the largest prompt any reader gets.
        widest = max(estimates.values(), key=lambda item: item["core_tokens"] + item["index_tokens"],
                     default={"core_tokens": 0, "index_tokens": 0})
        LOGGER.info("Community knowledge loaded: %d files, %d sections; up to %d core + %d index tokens",
                    len(counts), sum(counts.values()), widest["core_tokens"], widest["index_tokens"])
        if omitted:
            LOGGER.warning("Community knowledge core exceeds 4000 tokens; omitted sections: %s",
                           "; ".join(sorted(omitted)))
        if fallback:
            LOGGER.warning("Community knowledge topic index exceeds %d tokens; using file counts "
                           "for visibility profiles: %s", TOPIC_INDEX_TOKEN_LIMIT, "; ".join(fallback))
        if index_omitted:
            LOGGER.warning("Community knowledge file index exceeds %d tokens; omitted files: %s",
                           TOPIC_INDEX_TOKEN_LIMIT, "; ".join(sorted(index_omitted)))


def _topic_index(sections):
    files = {}
    for section in sections:
        files.setdefault(section.file_name, []).append(section.title)
    if not files:
        return "", 0, False, ()
    heading = "Knowledge topics (search_approved_knowledge for details):"
    text = "\n".join([heading, *(f"{name}:\n" + "\n".join(f"- {title}" for title in titles)
                                  for name, titles in files.items())])
    title_tokens = estimate_tokens(text)
    if title_tokens <= TOPIC_INDEX_TOKEN_LIMIT:
        return text, title_tokens, False, ()
    counts = [f"{name}: {len(titles)} sections" for name, titles in files.items()]
    text = "\n".join([heading, *counts])
    if estimate_tokens(text) <= TOPIC_INDEX_TOKEN_LIMIT:
        return text, title_tokens, True, ()
    # Even file names can exceed the cap in very large directories.
    note = "Additional files omitted; search for details."
    included, omitted = [heading], []
    for name, line in zip(files, counts):
        if estimate_tokens("\n".join([*included, line, note])) <= TOPIC_INDEX_TOKEN_LIMIT:
            included.append(line)
        else:
            omitted.append(name)
    return "\n".join([*included, note]), title_tokens, True, tuple(omitted)


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
        sections, counts = [], {}
        for path in paths:
            counts[path.name] = 0
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise ValueError("Knowledge file exceeds its size bound or is a symlink")
                parsed = _parse(path)
                sections.extend(parsed)
                counts[path.name] = len(parsed)
            except (OSError, UnicodeError, ValueError):
                LOGGER.exception("Cannot read community knowledge: %s", path.name)
        catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), tuple(sections)).prepare_prompts()
        catalog.log_reload(counts)
        self._signature = signature
        self._catalog = catalog
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
    always_loaded = metadata.get("always_loaded", False)
    if visibility not in VISIBILITIES or status not in STATUSES:
        raise ValueError("Invalid knowledge visibility or status")
    if type(always_loaded) is not bool:
        raise ValueError("Knowledge always_loaded must be a boolean")
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
                sections.append(KnowledgeSection(title, "\n".join(body).strip(), visibility,
                                                 path.name, always_loaded))
            title, body = heading[1], []
        else:
            body.append(line)
    if body or title != path.stem:
        sections.append(KnowledgeSection(title, "\n".join(body).strip(), visibility,
                                         path.name, always_loaded))
    return tuple(section for section in sections if section.body or section.title != path.stem)
