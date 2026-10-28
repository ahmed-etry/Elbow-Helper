"""Bounded temporary-file delivery for agent-owned workbook adapters."""

from __future__ import annotations

import asyncio
from typing import Any, Sequence


async def render_workbook_bytes(
    bot: Any, *, filesize_limit: int, temp_prefix: str,
    sheets: Sequence[tuple[str, Sequence[Sequence[Any]]]],
) -> bytes | None:
    """Render, bound and remove one temporary workbook."""
    def build() -> bytes | None:
        path = bot.local_exports.temporary_path(temp_prefix)
        try:
            bot.workbook_writer.write(path, sheets)
            if path.stat().st_size > filesize_limit:
                return None
            with path.open("rb") as file:
                data = file.read(filesize_limit + 1)
            return data if len(data) <= filesize_limit else None
        finally:
            bot.local_exports.delete(path)

    return await asyncio.to_thread(build)


async def publish_workbook_bytes(
    bot: Any, data: bytes, title: str,
) -> tuple[str | None, str | None]:
    """Publish the rendered bytes through the application-owned Google publisher."""
    publisher = getattr(bot, "google_publisher", None)
    if publisher is None:
        return None, None
    path = bot.local_exports.temporary_path("agent_google_spreadsheet")
    try:
        await asyncio.to_thread(path.write_bytes, data)
        return await publisher.upload_workbook(path, title)
    finally:
        await asyncio.to_thread(bot.local_exports.delete, path)
