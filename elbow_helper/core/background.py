"""Shared lifecycle policy for recurring background loops."""

from __future__ import annotations

import asyncio
from typing import Any

from discord.ext import tasks


def start_resilient_loop(loop: tasks.Loop[Any]) -> asyncio.Task[Any]:
    """Start a loop that logs, backs off, and retries unexpected failures."""

    loop.add_exception_type(Exception)
    return loop.start()
