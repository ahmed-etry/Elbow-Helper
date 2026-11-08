import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import sqlite3
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from elbow_helper.app import create_bot
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.ai import DeepSeekTextClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher, WorkbookWriter

async def check(project):
    project.mkdir(parents=True, exist_ok=True)
    previous = Path.cwd()
    os.chdir(project)
    try:
        loop_thread = get_ident()
        original_connect = sqlite3.connect
        def worker_connection(*args, **kwargs):
            if get_ident() == loop_thread:
                raise AssertionError("SQLite ran on the event loop")
            return original_connect(*args, **kwargs)
        bot = create_bot(ApplicationPaths.from_project_root(project), ClashClient(None),
                         DeepSeekTextClient(None), GoogleSheetsPublisher(client_id=None,
                         client_secret=None, refresh_token=None, folder_id=None), WorkbookWriter())
        bot.application_info = AsyncMock(return_value=SimpleNamespace(owner=None, team=None))
        with patch("sqlite3.connect", side_effect=worker_connection), patch("elbow_helper.core.lifecycle.sync_guild_commands", new=AsyncMock(return_value=())), patch.object(
                socket.socket, "connect", side_effect=AssertionError("Network disabled for startup rehearsal")):
            async with bot:
                await bot.setup_hook()
                print(json.dumps({"project": str(project), "extensions": len(bot.extensions),
                                  "cogs": len(bot.cogs), "startup": "passed"}), flush=True)
    finally:
        os.chdir(previous)

if __name__ == "__main__":
    asyncio.run(check(Path(sys.argv[1]).resolve()))
