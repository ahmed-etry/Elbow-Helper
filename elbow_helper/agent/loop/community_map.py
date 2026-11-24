"""Read tagged community explanations in full for one destination audience."""
from pathlib import Path

from elbow_helper.agent.knowledge.store import KnowledgeSection, VISIBILITIES


# Under the data folder, beside agent/knowledge; kept out of it so the notes store does not load it.
MEANINGS = Path("agent") / "map" / "meanings.md"


def load_meanings(data_root, levels):
    # No catalogue cache: the owner can revise this file between requests.
    path = Path(data_root) / MEANINGS
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    sections = []
    title = visibility = None
    body = []

    def finish():
        if visibility in {"public", *levels}:
            sections.append(KnowledgeSection(
                title=title, body="".join(body).rstrip(), visibility=visibility,
                file_name=path.name, always_loaded=True,
            ))

    for line in lines:
        if line.startswith("## "):
            finish()
            heading = line[3:].strip()
            title, separator, tag = heading.rpartition(" [")
            if not separator or not tag.endswith("]") or tag[:-1] not in VISIBILITIES:
                raise ValueError(f"Community map section needs a known visibility tag: {heading}")
            visibility = tag[:-1]
            body = []
        elif title is not None:
            body.append(line)
    finish()
    return sections
