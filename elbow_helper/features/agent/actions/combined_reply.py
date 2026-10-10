"""Independently controlled preview and answer parts in one Discord message."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from ..text import DISCORD_MESSAGE_LIMIT, chunk_response

LOGGER = logging.getLogger(__name__)
_UNSET = object()


class CombinedReplyView(discord.ui.View):
    def __init__(self, preview: str, answer: str, preview_view, answer_view,
                 *, preview_first: bool, on_change):
        super().__init__(timeout=None)
        self.parts = {"preview": preview, "answer": answer}
        self.order = ("preview", "answer") if preview_first else ("answer", "preview")
        self.views = {"preview": preview_view, "answer": answer_view}
        self.on_change = on_change
        self.message = None
        self.lock = asyncio.Lock()
        self.timers: dict[str, asyncio.Task] = {}
        self._rebuild()

    def render(self, **changes: str) -> str:
        parts = {**self.parts, **changes}
        return "\n\n".join(f"{index}. {parts[key]}" for index, key in enumerate(self.order, 1))

    def _rebuild(self) -> None:
        self.clear_items()
        for key in self.order:
            view = self.views[key]
            if view is not None:
                for item in view.children:
                    if not isinstance(item, discord.ui.Button) or item.url is None:
                        item.row = 0
                    self.add_item(item)

    def start(self, message) -> None:
        self.message = message
        for key in self.order:
            self._start_group(key)

    def _start_group(self, key: str) -> None:
        view = self.views[key]
        if view is None:
            return
        view.message = ReplyPartMessage(self, key)
        if view.timeout is not None and key not in self.timers:
            self.timers[key] = asyncio.create_task(self._expire_group(key, view))

    async def _expire_group(self, key, view) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(view.wait()), timeout=view.timeout)
        except TimeoutError:
            try:
                await view.on_timeout()
            except Exception:
                LOGGER.exception("Combined agent reply controls could not expire: part=%s", key)
            finally:
                view.stop()
        finally:
            if self.timers.get(key) is asyncio.current_task():
                self.timers.pop(key, None)
            self._stop_if_done()

    def _stop_if_done(self) -> None:
        if all(view is None or getattr(view, "expired", False) for view in self.views.values()):
            super().stop()

    async def edit_part(self, key: str, *, content=_UNSET, view=_UNSET, **kwargs):
        async with self.lock:
            await self.edit_part_locked(key, content=content, view=view, **kwargs)
        return ReplyPartMessage(self, key)

    async def edit_part_locked(self, key, *, content=_UNSET, view=_UNSET, **kwargs):
        """Edit one part while the caller holds the shared message lock."""
        previous = self.render()
        old_content, old_view = self.parts[key], self.views[key]
        if content is not _UNSET:
            self.parts[key] = content
        if view is not _UNSET:
            self.views[key] = view
        rendered = self.render()
        if len(rendered) > DISCORD_MESSAGE_LIMIT:
            self.parts[key], self.views[key] = old_content, old_view
            raise ValueError("Combined reply exceeds the message limit")
        self._rebuild()
        try:
            edited = await self.message.edit(
                content=rendered, view=self, allowed_mentions=discord.AllowedMentions.none(), **kwargs,
            )
        except BaseException:
            self.parts[key], self.views[key] = old_content, old_view
            self._rebuild()
            raise
        self.message = edited
        if old_view is not self.views[key]:
            timer = self.timers.pop(key, None)
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
            if old_view is not None:
                old_view.stop()
            self._start_group(key)
        if rendered != previous:
            await self.on_change(self.message, previous, rendered)
        self._stop_if_done()

    def stop(self) -> None:
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        for view in self.views.values():
            if view is not None:
                view.stop()
        super().stop()


class ReplyPartMessage:
    """A message-like edit target whose content is only one combined reply part."""

    preserves_other_text = True

    def __init__(self, reply: CombinedReplyView, key: str):
        self.reply = reply
        self.key = key

    @property
    def id(self):
        return self.reply.message.id

    @property
    def content(self):
        return self.reply.parts[self.key]

    async def edit(self, *, content=_UNSET, view=_UNSET, **kwargs):
        return await self.reply.edit_part(self.key, content=content, view=view, **kwargs)

    async def replace_report(self, report: str, private_view: Any):
        async with self.reply.lock:
            available = DISCORD_MESSAGE_LIMIT - len(self.reply.render(**{self.key: ""}))
            if len(report) <= available:
                chunks = [report]
            else:
                boundary = report.rfind("\n", 0, available)
                if boundary <= 0:
                    boundary = report.rfind(" ", 0, available)
                if boundary <= 0:
                    boundary = available
                chunks = [report[:boundary].rstrip(), *chunk_response(report[boundary:].lstrip())]
            await self.reply.edit_part_locked(
                self.key, content=chunks[0], view=private_view if len(chunks) == 1 else None,
            )
            return chunks[1:]
