"""Text evidence shared by local context and retrieved Discord messages."""

import re

import discord

from .identity import member_identity
from .models import AgentIdentity


def message_text(
    message: discord.Message, *, identity: AgentIdentity | None = None,
) -> str:
    parts = [str(message.content or "").strip()]
    for sticker in getattr(message, "stickers", ()):
        parts.append(f"[sticker: {sticker.name}]")
    for embed in getattr(message, "embeds", ()):
        author = getattr(getattr(embed, "author", None), "name", None)
        if author:
            parts.append(str(author))
        parts.extend(str(value) for value in (embed.title, embed.description) if value)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        footer = getattr(getattr(embed, "footer", None), "text", None)
        if footer:
            parts.append(str(footer))
    if message.attachments:
        names = ", ".join(item.filename for item in message.attachments)
        parts.append(f"[attachments, contents not read: {names}]")
    if any(
        str(getattr(item, "content_type", "") or "").startswith("image/")
        or str(getattr(item, "filename", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif"),
        )
        for item in getattr(message, "attachments", ())
    ):
        parts.append("[image]")
    if any(
        getattr(embed, "type", None) in ("image", "gifv", "rich")
        and (
            getattr(getattr(embed, "image", None), "proxy_url", None)
            or getattr(getattr(embed, "thumbnail", None), "proxy_url", None)
        )
        for embed in getattr(message, "embeds", ())
    ):
        parts.append("[image]")
    return render_member_mentions(
        "\n".join(part for part in parts if part), message, identity=identity,
    )


def render_member_mentions(
    content: str, message: discord.Message, *, identity: AgentIdentity | None = None,
) -> str:
    mentioned = {member.id: member for member in getattr(message, "mentions", ())}
    get_member = getattr(getattr(message, "guild", None), "get_member", None)

    def render(match: re.Match[str]) -> str:
        member_id = int(match.group(1))
        if identity is not None and member_id == identity.member_id:
            return f"@{identity.display_name} (you, member_id={member_id})"
        member = member_identity(mentioned.get(member_id))
        if member is None and callable(get_member):
            member = member_identity(get_member(member_id))
        if member is None or member.member_id != member_id:
            return match.group(0)
        return f"@{member.display_name} (member_id={member_id})"

    return re.sub(r"<@!?(\d+)>", render, content)


DISCORD_MESSAGE_LIMIT = 2_000


def _table_cells(line):
    """Split table pipes outside inline code, preserving escaped literal pipes."""
    cells, cell = [], []
    code_ticks = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            cell.extend(line[index:index + 2])
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(line) and line[end] == "`":
                end += 1
            count = end - index
            code_ticks = 0 if count == code_ticks else count if not code_ticks else code_ticks
            cell.append(line[index:end])
            index = end
            continue
        if char == "|" and not code_ticks:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        index += 1
    cells.append("".join(cell).strip())
    if line.lstrip().startswith("|"):
        cells.pop(0)
    if line.rstrip().endswith("|") and not line.rstrip().endswith("\\|"):
        cells.pop()
    return cells


def discord_text(content):
    """Render genuine Markdown tables as readable bullets; keep code and other text."""
    lines = content.splitlines()
    result = []
    fence = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            result.append(line)
            index += 1
            continue
        if fence is None and index + 2 < len(lines) and "|" in line:
            headings = _table_cells(line)
            separators = _table_cells(lines[index + 1])
            rows, end = [], index + 2
            if (len(headings) > 1 and len(headings) == len(separators)
                    and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators)):
                while end < len(lines) and "|" in lines[end]:
                    cells = _table_cells(lines[end])
                    if len(cells) != len(headings):
                        break
                    rows.append(cells)
                    end += 1
            if rows:
                for cells in rows:
                    result.append("- " + "; ".join(
                        f"{heading}: {value}" if heading else value
                        for heading, value in zip(headings, cells)
                    ))
                index = end
                continue
        result.append(line)
        index += 1
    # Avoid changing whitespace in replies that did not contain a table.
    return "\n".join(result) if result != lines else content


def chunk_response(content: str) -> list[str]:
    remaining = str(content or "").strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= DISCORD_MESSAGE_LIMIT:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = DISCORD_MESSAGE_LIMIT
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:DISCORD_MESSAGE_LIMIT])
        remaining = remaining[split_at:].lstrip()
    return chunks
