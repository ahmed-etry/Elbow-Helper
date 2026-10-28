"""Application-owned, coalescing Discord publishing.

Submission is synchronous and returns a future of PublishResult. Pending message
and order versions are replaced; pending channel fields are merged. Replaced
callers wait for the replacement's result. An edit already being sent finishes
independently. Cancelling a caller never cancels publishing or another caller.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from io import BytesIO
import logging
import time
from typing import Any, Awaitable, Callable

import discord


LOGGER = logging.getLogger(__name__)
# Discord's name/topic sublimit: https://github.com/discord/discord-api-docs/issues/2208
CHANNEL_EDIT_LIMIT = 2
CHANNEL_EDIT_WINDOW = 600.0
HTTP_ATTEMPTS = 3
HTTP_RETRY_DELAYS = (1.0, 2.0)
SHUTDOWN_TIMEOUT = 3.0


@dataclass(frozen=True)
class PublishResult:
    applied: bool
    value: Any = None
    error: Exception | None = None

    def __bool__(self) -> bool:
        return self.applied


@dataclass(frozen=True)
class _Upload:
    data: bytes
    filename: str
    spoiler: bool
    description: str | None

    def file(self) -> discord.File:
        return discord.File(BytesIO(self.data), filename=self.filename,
                            spoiler=self.spoiler, description=self.description)


def _snapshot_uploads(fields: dict[str, Any]) -> dict[str, Any]:
    """Read and release single-use discord.Files off the event loop."""
    result = dict(fields)
    if "attachments" in fields:
        attachments = []
        for attachment in fields["attachments"]:
            if isinstance(attachment, discord.File):
                try:
                    attachment.reset()
                    attachments.append(_Upload(attachment.fp.read(), attachment.filename,
                                               attachment.spoiler, attachment.description))
                finally:
                    attachment.close()
            else:
                attachments.append(attachment)
        result["attachments"] = attachments
    return result


def _signature(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _signature(item) for key, item in value.items() if key != "reason"}
    if isinstance(value, (list, tuple)):
        return [_signature(item) for item in value]
    if isinstance(value, discord.File):
        return value
    if isinstance(value, (discord.ui.View, discord.ui.LayoutView)):
        return _signature(value.to_components())
    if isinstance(value, (discord.Embed, discord.AllowedMentions, discord.Attachment)):
        return _signature(value.to_dict())
    if isinstance(value, discord.abc.Snowflake):
        return value.id
    return value


@dataclass
class _Item:
    target: Any
    fields: dict[str, Any]
    waiters: list[asyncio.Future]
    operation: Callable[[], Awaitable[Any]] | None = None
    prepared: bool = False


@dataclass
class _Destination:
    kind: str
    pending: _Item | None = None
    active: _Item | None = None
    worker: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    last: dict[str, Any] | None = None
    value: Any = None
    edits: deque[float] = field(default_factory=deque)
    blocked: PublishResult | None = None
    warned: bool = False


class PublishingQueue:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._destinations: dict[tuple, _Destination] = {}
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._retired_files: list[discord.File] = []

    def edit_message(self, message: discord.Message, **fields: Any) -> Awaitable[PublishResult]:
        return self._submit(("message", message.id), message, fields)

    def edit_channel(self, channel: discord.abc.GuildChannel, **fields: Any) -> Awaitable[PublishResult]:
        return self._submit(("channel", channel.id), channel, fields)

    def reorder(self, guild_id: int, group: Any, order: Any,
                operation: Callable[[], Awaitable[Any]]) -> Awaitable[PublishResult]:
        """Apply a complete desired order using discord.py's scoped move API."""
        return self._submit(("order", guild_id, group), None, {"order": order}, operation)

    def channel_pending(self, channel_id: int) -> bool:
        destination = self._destinations.get(("channel", channel_id))
        return destination is not None and (destination.pending is not None or destination.active is not None)

    def order_pending(self, guild_id: int, group: Any) -> bool:
        destination = self._destinations.get(("order", guild_id, group))
        return destination is not None and (destination.pending is not None or destination.active is not None)

    def _retire_files(self, item: _Item, replacement: _Item | None = None) -> None:
        retained = replacement.fields.get("attachments", ()) if replacement else ()
        self._retired_files.extend(
            attachment for attachment in item.fields.get("attachments", ())
            if isinstance(attachment, discord.File)
            and all(attachment is not other for other in retained)
        )

    def _submit(self, key: tuple, target: Any, fields: dict[str, Any],
                operation: Callable[[], Awaitable[Any]] | None = None) -> Awaitable[PublishResult]:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        item = _Item(target, fields, [waiter], operation)
        destination = self._destinations.setdefault(key, _Destination(key[0]))
        if self._closing or destination.blocked is not None:
            self._retire_files(item)
            waiter.set_result(destination.blocked if destination.blocked is not None else PublishResult(False))
            loop.create_task(self._dispose_files())
        else:
            previous = destination.pending
            if previous is not None:
                item.waiters = previous.waiters + item.waiters
                if destination.kind == "channel":
                    item.fields = previous.fields | item.fields
                self._retire_files(previous, item)
            destination.pending = item
            destination.changed.set()
            if destination.worker is None or destination.worker.done():
                destination.worker = loop.create_task(self._run(key, destination))
        return asyncio.shield(waiter)

    @staticmethod
    def _resolve(item: _Item | None, result: PublishResult) -> None:
        if item is not None:
            for waiter in item.waiters:
                if not waiter.done():
                    waiter.set_result(result)

    async def _dispose_files(self) -> None:
        files, self._retired_files = self._retired_files, []
        if files:
            await asyncio.to_thread(self._close_files, files)

    @staticmethod
    def _close_files(files: list[discord.File]) -> None:
        for attachment in files:
            attachment.close()

    def _fail(self, key: tuple, destination: _Destination, error: Exception,
              *, permanent: bool = False) -> None:
        result = PublishResult(False, error=error)
        self._resolve(destination.active, result)
        if destination.active:
            self._retire_files(destination.active)
        if permanent:
            destination.blocked = result
            self._resolve(destination.pending, result)
            if destination.pending:
                self._retire_files(destination.pending)
            destination.pending = None
        if not destination.warned:
            LOGGER.warning("Discord publishing failed destination=%s: %s", key, error)
            destination.warned = True

    async def _run(self, key: tuple, destination: _Destination) -> None:
        attempts = 0
        retry_at = 0.0
        try:
            while destination.pending is not None:
                await self._dispose_files()
                item = destination.pending
                signature = _signature(item.fields)
                identical = destination.last is not None and (
                    all(destination.last.get(field, object()) == value
                        for field, value in signature.items())
                    if destination.kind == "channel" else destination.last == signature
                )
                if identical:
                    destination.pending = None
                    self._retire_files(item)
                    self._resolve(item, PublishResult(True, destination.value))
                    continue
                now = self._clock()
                while destination.edits and now - destination.edits[0] >= CHANNEL_EDIT_WINDOW:
                    destination.edits.popleft()
                ready_at = retry_at
                limited = destination.kind == "channel" and bool({"name", "topic"} & item.fields.keys())
                if limited and len(destination.edits) >= CHANNEL_EDIT_LIMIT:
                    ready_at = max(ready_at, destination.edits[0] + CHANNEL_EDIT_WINDOW)
                if ready_at > now:
                    destination.changed.clear()
                    try:
                        async with asyncio.timeout(ready_at - now):
                            await destination.changed.wait()
                    except TimeoutError:
                        pass
                    continue
                destination.pending = None
                destination.active = item
                try:
                    if not item.prepared:
                        if any(isinstance(value, discord.File)
                               for value in item.fields.get("attachments", ())):
                            item.fields = await asyncio.to_thread(_snapshot_uploads, item.fields)
                        item.prepared = True
                    signature = _signature(item.fields)
                    if destination.kind == "message" and destination.last == signature:
                        self._resolve(item, PublishResult(True, destination.value))
                        destination.active = None
                        continue
                    if limited:
                        destination.edits.append(self._clock())
                    if item.operation is not None:
                        value = await item.operation()
                        if value is False:
                            raise RuntimeError("Channel order could not be applied")
                    else:
                        fields = dict(item.fields)
                        uploads = []
                        if "attachments" in fields:
                            fields["attachments"] = [
                                attachment.file() if isinstance(attachment, _Upload) else attachment
                                for attachment in fields["attachments"]
                            ]
                            uploads = [attachment for attachment in fields["attachments"]
                                       if isinstance(attachment, discord.File)]
                        try:
                            value = await item.target.edit(**fields)
                        finally:
                            self._close_files(uploads)
                except (discord.NotFound, discord.Forbidden) as error:
                    self._fail(key, destination, error, permanent=True)
                except discord.HTTPException as error:
                    attempts += 1
                    if attempts >= HTTP_ATTEMPTS:
                        self._fail(key, destination, error)
                        attempts = 0
                    else:
                        newer = destination.pending
                        if newer is None:
                            destination.pending = item
                        else:
                            newer.waiters = item.waiters + newer.waiters
                            if destination.kind == "channel":
                                newer.fields = item.fields | newer.fields
                        retry_at = self._clock() + HTTP_RETRY_DELAYS[attempts - 1]
                except Exception as error:
                    self._fail(key, destination, error)
                    attempts = 0
                    retry_at = 0.0
                else:
                    if destination.kind == "channel":
                        destination.last = (destination.last or {}) | signature
                    else:
                        destination.last = signature
                    destination.value = value
                    self._resolve(item, PublishResult(True, value))
                    attempts = 0
                    retry_at = 0.0
                destination.active = None
        except Exception as error:
            # Even invalid payloads must settle their callers, including errors
            # encountered while comparing a pending version with the last edit.
            if destination.active is None:
                destination.active = destination.pending
                destination.pending = None
            self._fail(key, destination, error)
            self._resolve(destination.pending, PublishResult(False, error=error))
            if destination.pending:
                self._retire_files(destination.pending)
            destination.pending = None
        finally:
            if destination.active is not None:
                self._resolve(destination.active, PublishResult(False))
                destination.active = None
            # File cleanup may yield after callers have received a result. Let
            # a later submission start its worker while that cleanup finishes.
            destination.worker = None
            await self._dispose_files()

    async def close(self) -> None:
        """Flush while Discord is open, then resolve every remaining waiter."""
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._flush())
        await asyncio.shield(self._close_task)

    async def _flush(self) -> None:
        workers = [destination.worker for destination in self._destinations.values()
                   if destination.worker is not None and not destination.worker.done()]
        if workers:
            _, remaining = await asyncio.wait(workers, timeout=SHUTDOWN_TIMEOUT)
            if remaining:
                dropped = sum(destination.pending is not None or destination.active is not None
                              for destination in self._destinations.values())
                LOGGER.warning("Discord publishing shutdown dropped %s destinations", dropped)
                for worker in remaining:
                    worker.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
        for destination in self._destinations.values():
            self._resolve(destination.pending, PublishResult(False))
            if destination.pending:
                self._retire_files(destination.pending)
            destination.pending = None
        await self._dispose_files()
