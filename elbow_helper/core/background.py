"""Shared lifecycle policy for recurring background loops."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
from typing import Any

from discord.ext import tasks


def start_resilient_loop(loop: tasks.Loop[Any]) -> asyncio.Task[Any]:
    """Start a loop that logs, backs off, and retries unexpected failures."""

    loop.add_exception_type(Exception)
    return loop.start()


def start_tracked_task(
    coroutine: Coroutine[Any, Any, None],
    *,
    name: str,
    tracked: set[asyncio.Task[None]],
    logger: logging.Logger,
    label: str,
) -> asyncio.Task[None]:
    """Run a coroutine, keep it in `tracked` until done, and log any failure."""

    task = asyncio.create_task(coroutine, name=name)
    tracked.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        tracked.discard(completed)
        try:
            completed.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("%s background task failed: %s", label, name)

    task.add_done_callback(finish)
    return task
