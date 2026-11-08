"""Copy one validated empty schema for tests of record behavior.

Startup and migration tests still open an ordinary, uninitialized FamilyStore.
Each caller owns a separate real file, connection boundary and writer lock.
"""

import asyncio
from pathlib import Path
import shutil
from threading import Lock

from elbow_helper.data.store import FamilyStore
from tests.temp_directory import TemporaryDirectory


_lock = Lock()
_template_directory = None


def _copy_empty_schema(path):
    global _template_directory
    with _lock:
        if _template_directory is None:
            directory = TemporaryDirectory(prefix="family-schema-")
            try:
                asyncio.run(FamilyStore(Path(directory.name) / "empty.sqlite3").initialize())
            except BaseException:
                directory.cleanup()
                raise
            _template_directory = directory
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(_template_directory.name) / "empty.sqlite3", path)


async def empty_store(path):
    path = Path(path)
    await asyncio.to_thread(_copy_empty_schema, path)
    store = FamilyStore(path)
    store._ready = True  # Only schema construction is shared; every test starts empty.
    return store
