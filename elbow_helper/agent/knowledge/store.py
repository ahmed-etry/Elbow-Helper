"""Approved Markdown sections, reloaded when files change."""

from dataclasses import dataclass, field, replace
from collections import Counter
from datetime import datetime, timezone
import logging
from math import log1p
from pathlib import Path
import re
import time
import tomllib

from ..conversation.context import estimate_tokens


LOGGER = logging.getLogger(__name__)
VISIBILITIES = frozenset({"public", "lead", "lead_plus", "core", "recruiter", "cwl_helper", "examiner"})
STATUSES = frozenset({"approved", "draft", "retired"})
TOPIC_INDEX_TOKEN_LIMIT = 14_000
SECTION_TOKEN_TARGET = 1_500
HEADING_SEPARATOR = " › "


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


_STOP_WORDS = frozenset(_words(
    "a about above after again against all am an and any are as at be because been before "
    "being below between both but by can could did do does doing down during each few for "
    "from further had has have having he her here hers herself him himself his how i if in "
    "into is it its itself just me more most my myself no nor not of off on once only or "
    "other our ours ourselves out over own same she should so some such than that the their "
    "theirs them themselves then there these they this those through to too under until up "
    "very was we were what when where which while who whom why will with would you your "
    "yours yourself yourselves s t don doesn didn isn aren wasn weren won wouldn couldn "
    "shouldn hasn haven hadn mustn d ll m re ve"
))


def _bm25(frequency, length, average_length):
    """Saturate term frequency and normalize field length (k1=1.2, b=0.75)."""
    return 2.2 * frequency / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    title: str
    body: str
    visibility: str = "public"
    file_name: str = ""
    always_loaded: bool = False
    part: int = 1
    parts: int = 1
    applicability: str = ""


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
    store_status: str = "available"
    _prompts: dict[frozenset[str], KnowledgePrompt] = field(default_factory=dict, repr=False,
                                                         compare=False)

    def matching_titles(self, sections, titles):
        """Open full paths, or a leaf title identifying one visible section path."""
        selected = []
        for title in dict.fromkeys(value.casefold() for value in titles):
            matches = tuple(section for section in sections if section.title.casefold() == title)
            if not matches:
                matches = tuple(section for section in sections
                                if section.title.rsplit(HEADING_SEPARATOR, 1)[-1].casefold() == title)
                if len({(section.file_name, section.title.casefold()) for section in matches}) != 1:
                    continue
            selected.extend(section for section in matches if section not in selected)
        return tuple(selected)

    def select(self, sections, *, titles=(), query=""):
        """Open headings exactly; indexed file names narrow a lexical search.

        A filename is a container in the topic index, not a section title. Never
        let it silently discard the accompanying query or widen visibility.
        """
        sections = tuple(sections)
        if not titles:
            return self.search(sections, query=query, limit=None), (
                "lexical_ranked" if query.strip() else "browse")
        exact = self.matching_titles(sections, titles)
        files = {value.casefold() for value in titles}
        contained = tuple(section for section in sections if section.file_name.casefold() in files)
        ranked = self.search(contained, query=query, limit=None) if contained else ()
        return tuple(dict.fromkeys((*exact, *ranked))), (
            "lexical_ranked" if contained and query.strip() else "exact_titles")

    def search(self, sections, *, query="", limit=8):
        phrase = _words(query)
        sections = tuple(sections)
        if not phrase:
            return sections[:limit]
        words = set(phrase) - _STOP_WORDS
        documents = []
        for section in sections:
            title_words = _words(section.title)
            title = Counter(word for word in title_words if word not in _STOP_WORDS)
            body = Counter(word for word in _words(section.body) if word not in _STOP_WORDS)
            documents.append((section, title_words, title, body))
        if not documents:
            return ()
        average_title = sum(title.total() for _, _, title, _ in documents) / len(documents) or 1
        average_body = sum(body.total() for _, _, _, body in documents) / len(documents) or 1
        frequencies = Counter(word for _, _, title, body in documents for word in words
                              if word in title or word in body)
        weights = {word: log1p((len(documents) - count + 0.5) / (count + 0.5))
                   for word, count in frequencies.items()}
        ranked = []
        for index, (section, title_words, title, body) in enumerate(documents):
            title_length, body_length = title.total(), body.total()
            score = sum(weight * (
                3 * _bm25(title[word], title_length, average_title)
                + _bm25(body[word], body_length, average_body))
                for word, weight in weights.items() if word in title or word in body)
            exact = phrase == title_words
            if exact or score:
                ranked.append((-exact, -score, index, section))
        return tuple(item[-1] for item in sorted(ranked)[:limit])

    def visible_sections(self, levels=()):
        return tuple(section for section in self.active_sections
                     if section.visibility == "public" or section.visibility in levels)

    def prompt(self, levels=()):
        key = frozenset(levels) & (VISIBILITIES - {"public"})
        if key not in self._prompts:
            self._prompts[key] = self._build_prompt(key)
        return self._prompts[key]

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
        # Titles and file counts are evidence under the same destination boundary.
        access.update(section.visibility for section in visible if section.visibility != "public")
        index, title_tokens, fallback, index_omitted = _topic_index(visible)
        return KnowledgePrompt(core, frozenset(access), estimate_tokens(core), tuple(omitted),
                               index, estimate_tokens(index), title_tokens, fallback, index_omitted)

    def prepare_prompts(self):
        # Prepare bounded reload diagnostics; cache actual reader profiles on demand.
        catalog = replace(self, _prompts={})
        catalog.prompt()
        catalog.prompt(VISIBILITIES - {"public"})
        return catalog

    def log_reload(self, counts):
        """Report each reload once using the prepared visibility profiles."""
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
        titles = files.setdefault(section.file_name, {})
        titles[section.title] = titles.get(section.title, 0) + 1
    if not files:
        return "", 0, False, ()
    heading = "Knowledge topics (search_approved_knowledge for details):"
    lines = [heading]
    for name, titles in files.items():
        lines.append(f"{name}:")
        lines.extend(f"- {title}" + (f" ({parts} parts)" if parts > 1 else "")
                     for title, parts in titles.items())
    text = "\n".join(lines)
    title_tokens = estimate_tokens(text)
    if title_tokens <= TOPIC_INDEX_TOKEN_LIMIT:
        return text, title_tokens, False, ()
    counts = [f"{name}: {sum(titles.values())} sections" for name, titles in files.items()]
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
        self._catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), (), "unavailable")

    def load(self):
        now = time.monotonic()
        if now - self._checked_at < 60:
            return self._catalog
        self._checked_at = now
        try:
            if not self.directory.is_dir():
                raise OSError("Knowledge directory is unavailable")
            paths = sorted(self.directory.glob("*.md"), key=lambda path: path.name.casefold())
            signature = tuple(
                (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths
            )
        except OSError:
            LOGGER.exception("Cannot inspect community knowledge files")
            self._signature = None
            self._catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), (), "unavailable")
            return self._catalog
        if signature == self._signature:
            return self._catalog
        sections, counts, incomplete = [], {}, False
        for path in paths:
            counts[path.name] = 0
            try:
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    raise ValueError("Knowledge file exceeds its size bound or is a symlink")
                parsed = tuple(_parse(path))
                sections.extend(parsed)
                counts[path.name] = len(parsed)
            except (OSError, UnicodeError, ValueError):
                incomplete = True
                LOGGER.exception("Cannot read community knowledge: %s", path.name)
        catalog = KnowledgeCatalog(datetime.now(timezone.utc).isoformat(), tuple(sections),
                                   "partial" if incomplete else "available").prepare_prompts()
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
    headings = []
    body = []
    fence = None

    def flush():
        content = "\n".join(body).strip()
        if not content:
            return
        title = HEADING_SEPARATOR.join(heading for _, heading in headings) or path.stem
        first_line = content.splitlines()[0]
        applicability = first_line.removeprefix('Applies to:').strip() if first_line.startswith('Applies to:') else ''
        chunks = _section_parts(title, content)
        sections.extend(KnowledgeSection(title, chunk, visibility, path.name, always_loaded,
                                         index, len(chunks), applicability)
                        for index, chunk in enumerate(chunks, 1))

    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            body.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if heading:
            flush()
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2]))
            body = []
        else:
            body.append(line)
    flush()
    return tuple(sections)


def _section_parts(title, body):
    """Keep paragraphs and fenced blocks whole, allowing one oversized block."""
    paragraphs, lines, fence = [], [], None
    for line in body.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        if not line.strip() and fence is None:
            if lines:
                paragraphs.append("\n".join(lines))
                lines = []
        else:
            lines.append(line)
    if lines:
        paragraphs.append("\n".join(lines))
    chunks, current = [], ""
    for paragraph in paragraphs:
        combined = current + "\n\n" + paragraph if current else paragraph
        if current and estimate_tokens(title + "\n" + combined) > SECTION_TOKEN_TARGET:
            chunks.append(current)
            current = paragraph
        else:
            current = combined
    if current:
        chunks.append(current)
    return tuple(chunks)
