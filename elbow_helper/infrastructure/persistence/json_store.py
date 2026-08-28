"""Low-level JSON file persistence with crash-safe replacement."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Any


def read_json(path_like: str | os.PathLike[str]) -> Any:
    """Read one UTF-8 JSON document without applying feature-level defaults."""

    path = Path(path_like)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


async def read_json_async(path_like: str | os.PathLike[str]) -> Any:
    """Read a JSON document in a worker thread."""

    return await asyncio.to_thread(read_json, path_like)


def write_json_atomic(
    path_like: str | os.PathLike[str],
    payload: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = True,
) -> None:
    """Write JSON without exposing a partially written destination file."""

    path = Path(path_like)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(
                payload,
                handle,
                indent=indent,
                ensure_ascii=ensure_ascii,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


async def write_json_atomic_async(
    path_like: str | os.PathLike[str],
    payload: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = True,
) -> None:
    """Write a JSON document atomically in a worker thread."""

    await asyncio.to_thread(
        write_json_atomic,
        path_like,
        payload,
        indent=indent,
        ensure_ascii=ensure_ascii,
    )
