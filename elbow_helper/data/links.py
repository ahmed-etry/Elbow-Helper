"""One-time legacy import and atomic family-record ownership changes."""

import asyncio
from contextlib import closing
from pathlib import Path
import logging
import sqlite3
from uuid import uuid4

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .store import now_us
from .store.legacy_links import TOPIC, FIELDS, LinkConflict, digest
from .store.link_state import apply_rows, compatibility, patched, receipt, display_rows
from .store.people_import import import_people
from .store.projections import canonical_json, link_projection

LOGGER = logging.getLogger(__name__)


class LinkService:
    def __init__(self, store, legacy_path):
        self.store = store
        self.legacy_path = Path(legacy_path)
        self._started = False
        self._writer = asyncio.Lock()
        self._jobs = set()

    async def _serialize(self, operation, *args):
        # Cancellation cannot let a second change overtake a finishing commit.
        async def run():
            async with self._writer:
                return await self.store.call(operation, *args)
        task = asyncio.create_task(run())
        self._jobs.add(task)
        def finished(task):
            self._jobs.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def close(self):
        while self._jobs:
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)

    async def startup(self, *, at=None):
        try:
            return await self._serialize(self._startup, at)
        except Exception as error:
            self._started = False
            LOGGER.warning("Account links unavailable until the people import succeeds: %s", error)
            return {"ready": False, "reason": str(error)}

    async def retry_import(self):
        if not self._started:
            return await self.startup()
        return {"ready": True, "imported": False}

    def _startup(self, connection, at):
        # Later boots never inspect the old links after the receipt is present.
        if connection.execute("SELECT 1 FROM imports WHERE topic=?", (TOPIC,)).fetchone():
            self._require_ready(connection)
            result = {"imported": False}
        else:
            result = import_people(connection, self.legacy_path, at=at)
        self._started = True
        return {**result, "ready": True}

    @staticmethod
    def _require_ready(connection):
        from .reads.people_links import require_links
        require_links(connection)

    async def read_links(self):
        return await asyncio.to_thread(self.read_links_in_worker)

    def read_links_in_worker(self, select=None):
        from .reads.people import PeopleReads
        return PeopleReads(self.store).links_in_worker(select)

    async def read_people(self, operation, *args):
        return await self._serialize(self._read_people, operation, args)

    def _read_people(self, connection, operation, args):
        self._require_ready(connection)
        return operation(connection, *args)

    async def restore_links(self, before, *, operation_key=None, at=None):
        return await self._write_rows(before, operation_key, at, "restore")

    async def _write_rows(self, requested, operation_key, at, kind):
        rows = {}
        for raw_tag, row in requested.items():
            tag = normalize_player_tag(raw_tag)
            if tag is None or tag in rows:
                raise ValueError("invalid_or_duplicate_link_tag")
            if row is not None:
                if kind == "refresh":
                    row = {key: row[key] for key in FIELDS[2:]}
                else:
                    row = {"player_tag": tag, "discord_user_id": int(row["discord_user_id"]),
                           **{key: row.get(key, "") for key in FIELDS[2:]}}
                    link_projection({tag: row})
            rows[tag] = row
        if at is not None and (type(at) is not int or at < 0):
            raise ValueError("invalid_operation_time")
        key = uuid4().hex if operation_key is None else operation_key
        if not isinstance(key, str) or not key:
            raise ValueError("invalid_operation_key")
        return await self._serialize(self._mutate, rows, key, at, kind)

    async def upsert_links(self, links, *, operation_key=None, at=None):
        rows = {}
        for row in links:
            tag = normalize_player_tag(row["player_tag"])
            if tag is None:
                raise ValueError("invalid_link_tag")
            rows[tag] = {"player_tag": tag, "discord_user_id": int(row["discord_user_id"]),
                         **{key: str(row.get(key) or "") for key in FIELDS[2:]}}
        return await self._write_rows(rows, operation_key, at, "upsert")

    async def upsert_link(self, *, operation_key=None, at=None, **row):
        return await self.upsert_links([row], operation_key=operation_key, at=at)

    async def delete_links(self, tags, *, operation_key=None, at=None):
        return await self._write_rows(dict.fromkeys(tags), operation_key, at, "delete")

    async def delete_link(self, tag, *, operation_key=None, at=None):
        return await self.delete_links([tag], operation_key=operation_key, at=at)

    async def update_link_last_seen(self, *, player_tag, operation_key=None, at=None, **metadata):
        if set(metadata) != set(FIELDS[2:]) or any(
                value is not None and not isinstance(value, str) for value in metadata.values()):
            raise ValueError("invalid_link_metadata")
        return await self._write_rows({player_tag: metadata}, operation_key, at, "refresh")

    def _mutate(self, connection, requested, operation_key, at, kind):
        result = self._commit(connection, requested, operation_key, at, kind)
        try:
            # Remove this rollback copy in the next update after this one ships.
            self._write_legacy(connection)
        except Exception as error:
            LOGGER.warning("Old account links write failed after %s operation %s: %s",
                           kind, operation_key, error)
        return result

    def _write_legacy(self, connection):
        rows = connection.execute("SELECT * FROM current_link_rows").fetchall()
        # Never read the old table, create a missing file, or wait on its locks.
        with closing(sqlite3.connect(self.legacy_path.resolve().as_uri() + "?mode=rw",
                                     uri=True, timeout=0)) as legacy:
            with sqlite_transaction(legacy, immediate=True):
                legacy.execute("DELETE FROM links")
                legacy.executemany(f"INSERT INTO links ({','.join(FIELDS)}) VALUES (?,?,?,?,?,?)",
                                   [tuple(row[field] for field in FIELDS) for row in rows])

    def _commit(self, connection, requested, operation_key, at, kind):
        connection.execute("PRAGMA synchronous=FULL")
        request_json = canonical_json({"kind": kind, "rows": requested})
        with sqlite_transaction(connection, immediate=True):
            self._require_ready(connection)
            existing = connection.execute("SELECT * FROM link_operations WHERE operation_key=?",
                                          (operation_key,)).fetchone()
            if existing:
                if existing["requested_json"] != request_json:
                    raise LinkConflict("operation_key_reused_with_different_request")
                if existing["status"] != "complete":
                    raise LinkConflict("aborted_or_stale_operation_key")
                return operation_key
            at = now_us() if at is None else at
            marker = connection.execute("SELECT frozen_at FROM imports WHERE topic=?", (TOPIC,)).fetchone()
            if at < marker[0]:
                raise LinkConflict("operation_precedes_import")
            current = compatibility(connection)
            if kind == "refresh":
                requested = {tag: {**current[tag], **{key: row[key] for key in FIELDS[2:]}}
                             for tag, row in requested.items() if tag in current}
            before = {tag: current.get(tag) for tag in requested}
            after = patched(current, requested)
            item = receipt(connection, "link_operation", operation_key, at, len(requested))
            apply_rows(connection, requested, at, item, basis="local_operation", operation_key=operation_key)
            display_rows(connection, requested, reorder=kind == "restore")
            connection.execute(
                "INSERT INTO link_operations(operation_key,requested_json,before_json,after_json,before_digest,"
                "after_digest,created_at,finished_at,status,source_item_id) VALUES (?,?,?,?,?,?,?,?,'complete',?)",
                (operation_key, request_json, canonical_json(before), canonical_json(requested),
                 digest(current), digest(after), at, at, item))
            connection.execute("UPDATE imports SET current_links_digest=?,checked_at=? WHERE topic=?",
                               (digest(after), at, TOPIC))
        return operation_key
