"""Application-owned nightly SQLite backup scheduling."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from pathlib import Path

from discord.ext import tasks

from elbow_helper.infrastructure.persistence.backups import backup_databases
from elbow_helper.infrastructure.persistence.backups import backups_need_catch_up

from .background import start_resilient_loop


BACKUP_TIME_UTC = time(hour=4, tzinfo=timezone.utc)


class DatabaseBackupJob:
    def __init__(self, data_root: Path, *, ready: asyncio.Event):
        self.data_root = data_root
        self.ready = ready

    async def __aenter__(self) -> DatabaseBackupJob:
        start_resilient_loop(self.daily)
        return self

    async def __aexit__(self, *_exc) -> None:
        self.daily.cancel()
        task = self.daily.get_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> None:
        worker = asyncio.create_task(asyncio.to_thread(backup_databases, self.data_root))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Wait for open connections and temporary files to be closed on exit.
            await worker
            raise

    @tasks.loop(time=BACKUP_TIME_UTC)
    async def daily(self) -> None:
        await self.run_once()

    @daily.before_loop
    async def prepare(self) -> None:
        await self.ready.wait()
        if await asyncio.to_thread(
            backups_need_catch_up, self.data_root, datetime.now(timezone.utc),
        ):
            await self.run_once()
