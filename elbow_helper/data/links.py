"""Independent legacy reads, with an optional ordered family-record mirror."""

import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import logging
import sqlite3
from threading import Lock
from uuid import uuid4

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .store import now_us
from .store.legacy_links import (
    TOPIC, FIELDS, ImportMismatch, LinkConflict, digest, snapshot,
)
from .store.link_state import (
    apply_rows, check_mapping, mapping_counts, operation_patch, patched, receipt, requested_patch,
)
from .store.people_import import import_people
from .store.projections import canonical_json, link_projection

LOGGER = logging.getLogger(__name__)


class LinkService:
    def __init__(self, store, legacy_path):
        self.store = store
        self.legacy_path = Path(legacy_path)
        self._started = False
        self._conflicted = False
        self._reported = set()
        self._writer = asyncio.Lock()
        self._access = Lock()
        self._jobs = set()
        self._image = None

    async def _serialize(self, operation, *args):
        # The task owns the lock even if its caller is cancelled. Disk work
        # finishes before another writer or a cheap legacy read can pass it.
        async def run():
            async with self._writer:
                return await asyncio.to_thread(operation, *args)
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

    def _family(self, operation, *args, **kwargs):
        """Hold the family writer only for this family transaction/check."""
        return self.store._call(operation, args, kwargs)

    def _fallback(self, error, *, conflict=False):
        self._started = False
        self._conflicted = self._conflicted or conflict
        category = "conflict" if conflict else "fallback"
        if category not in self._reported:
            self._reported.add(category)
            LOGGER.warning("Account link mirror %s; legacy reads and writes remain active: %s",
                           "conflict" if conflict else "deferred", error, exc_info=True)

    async def startup(self, *, at=None):
        """Try the checked switch without making legacy availability depend on it."""
        return await self._serialize(self._try_startup, at)

    async def retry_import(self):
        """Called after a complete collector generation, never during boot."""
        return await self._serialize(self._try_startup, None, True)

    def _try_startup(self, at, retry=False):
        if self._conflicted or retry and self._started:
            return {"mirrored": self._started}
        # Startup reads/checks do not change the legacy file. Keep ordinary
        # reads independent even while the family writer is busy. The async
        # writer lock prevents our legacy mutations during this freeze.
        self._started = False
        try:
            result = self._family(self._startup, at)
        except Exception as error:
            self._fallback(error, conflict=isinstance(error, LinkConflict)
                           and not isinstance(error, ImportMismatch))
            return {"mirrored": False, "reason": str(error)}
        self._started = True
        return result

    def _startup(self, connection, at):
        connection.execute("PRAGMA synchronous=FULL")
        self._recover(connection)
        frozen = snapshot(self.legacy_path, at)
        result = import_people(connection, self.legacy_path, frozen)
        self._image = frozen
        return {**result, "mirrored": True}

    async def recover(self):
        """Explicit recovery reports conflicts but always preserves legacy access."""
        return await self._serialize(self._recover_in_worker)

    def _recover_in_worker(self):
        with self._access:
            try:
                return self._family(self._recover_ready)
            except Exception as error:
                self._fallback(error, conflict=isinstance(error, LinkConflict))
                raise

    def _recover_ready(self, connection):
        count = self._recover(connection, durable=True)
        frozen = snapshot(self.legacy_path)
        marker = connection.execute("SELECT current_links_digest FROM imports WHERE topic=?", (TOPIC,)).fetchone()
        if marker is not None:
            if marker[0] != frozen.links_digest:
                raise LinkConflict("legacy_mapping_changed_outside_service")
            check_mapping(connection, frozen.normalized, constraints=True)
            self._image = frozen
            self._started = True
            self._conflicted = False
        return count

    def _recover(self, connection, *, durable=False):
        if durable:
            connection.execute("PRAGMA synchronous=FULL")
        pending = connection.execute("SELECT * FROM link_operations WHERE status='pending' ORDER BY id").fetchall()
        if len(pending) > 1:
            raise LinkConflict("multiple_pending_link_operations")
        for operation in pending:
            marker = connection.execute("SELECT frozen_at FROM imports WHERE topic=?", (TOPIC,)).fetchone()
            if marker is None or operation["created_at"] < marker[0]:
                raise LinkConflict("intent_crosses_unrecognized_rollback")
            frozen = snapshot(self.legacy_path)
            current = frozen.links_digest
            # A no-op batch has equal images. Completing it is idempotent and
            # changes no ownership; after-image takes precedence as specified.
            if current == operation["after_digest"]:
                self._finish(connection, operation, frozen, verify=True)
            elif current == operation["before_digest"]:
                with sqlite_transaction(connection, immediate=True):
                    connection.execute("UPDATE link_operations SET status='aborted',finished_at=? WHERE id=?",
                                       (now_us(), operation["id"]))
            else:
                raise LinkConflict("legacy_link_image_conflict:" + operation["operation_key"])
        return len(pending)

    def _finish(self, connection, operation, frozen, *, verify=False):
        connection.execute("PRAGMA synchronous=FULL")
        before = json.loads(operation["before_json"])
        after = json.loads(operation["after_json"])
        if (frozen.links_digest != operation["after_digest"]
                or any(frozen.links.get(tag) != row for tag, row in after.items())):
            raise LinkConflict("legacy_after_image_changed")
        expected_before = patched(frozen.links, before)
        if digest(expected_before) != operation["before_digest"]:
            raise LinkConflict("invalid_link_intent_images")
        normalized_before = {row["player_tag"]: row for row in link_projection(expected_before)}
        with sqlite_transaction(connection, immediate=True):
            # A pending intent has never changed family-visible ownership.
            if verify:
                check_mapping(connection, normalized_before, constraints=True)
            at = operation["created_at"]
            item = receipt(connection, "link_operation", operation["operation_key"], at, len(after))
            apply_rows(connection, operation_patch(operation), at, item, basis="local_operation",
                       operation_key=operation["operation_key"])
            checks = (check_mapping(connection, frozen.normalized, constraints=True) if verify
                      else mapping_counts(frozen.normalized))
            connection.execute("UPDATE imports SET current_links_digest=?,checked_at=?,checks_json=? WHERE topic=?",
                               (operation["after_digest"], now_us(), canonical_json(checks), TOPIC))
            connection.execute("UPDATE link_operations SET status='complete',finished_at=?,source_item_id=? WHERE id=?",
                               (now_us(), item, operation["id"]))

    def _require_ready(self, connection):
        if not self._started or connection.execute("SELECT 1 FROM imports WHERE topic=?", (TOPIC,)).fetchone() is None:
            raise LinkConflict("people_import_not_started")
        if connection.execute("SELECT 1 FROM link_operations WHERE status='pending'").fetchone():
            raise LinkConflict("link_recovery_required")

    async def read_links(self):
        """One legacy query, independent of the family record and collectors."""
        return await asyncio.to_thread(self.read_links_in_worker)

    def read_links_in_worker(self, select=None):
        """Synchronous facade entry to read_links; callers must use a worker."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            with self._access, closing(sqlite3.connect(
                    self.legacy_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
                connection.row_factory = sqlite3.Row
                return self._read(connection) if select is None else select(connection)
        raise RuntimeError("link_read_requires_worker")

    def _read(self, connection):
        return {str(row["player_tag"]): dict(row) for row in connection.execute("SELECT * FROM links")}

    async def restore_links(self, before, *, operation_key=None, at=None):
        """A batch and its undo each use one intent and one legacy commit."""
        return await self._write_rows(before, operation_key, at, "restore")

    async def _write_rows(self, before, operation_key, at, kind):
        rows = {}
        for raw_tag, row in before.items():
            tag = normalize_player_tag(raw_tag)
            if tag is None or tag in rows:
                raise ValueError("invalid_or_duplicate_link_tag")
            if row is not None:
                values = {key: row.get(key, "") for key in FIELDS[2:]}
                row = {"player_tag": tag, "discord_user_id": int(row["discord_user_id"]), **values}
                link_projection({tag: row})
            rows[tag] = row
        return await self._serialize(self._mutate, rows, operation_key or uuid4().hex, at, kind)

    async def upsert_links(self, links, *, operation_key=None, at=None):
        # Match the existing facade's upsert defaults and NULL-to-empty coercion.
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
        tag = normalize_player_tag(player_tag)
        if tag is None or set(metadata) != set(FIELDS[2:]):
            raise ValueError("invalid_link_metadata")
        if any(value is not None and not isinstance(value, str) for value in metadata.values()):
            raise ValueError("invalid_link_metadata")
        return await self._serialize(self._refresh, tag, metadata, operation_key or uuid4().hex, at)

    def _refresh(self, tag, metadata, operation_key, at):
        if self._started:
            old = self._image.normalized.get(tag)
            patch = {tag: {**old, **metadata}} if old else {}
        else:
            # UPDATE does not need the owner and must not create a missing row.
            patch = {tag: {"player_tag": tag, **metadata}}
        return self._mutate(patch, operation_key, at, "refresh")

    def _mutate(self, requested, operation_key, at, kind):
        if not isinstance(operation_key, str) or not operation_key:
            raise ValueError("invalid_operation_key")
        with self._access:
            if not self._started:
                self._apply_direct(requested, kind=kind)
                return operation_key
            frozen = replace(self._image, frozen_at=now_us() if at is None else at)
            before, after = requested_patch(frozen.links, requested)
            after_image = replace(frozen, links=patched(frozen.links, after))
            try:
                operation = self._family(self._intent, requested, operation_key, frozen, before, after, kind)
            except LinkConflict as error:
                if str(error) in ("operation_key_reused_with_different_request", "aborted_or_stale_operation_key"):
                    raise
                self._fallback(error, conflict=True)
                self._apply_direct(after, kind=kind)
                return operation_key
            except (OSError, sqlite3.Error) as error:
                # No legacy commit has happened yet. Keep this change usable
                # even when the optional record cannot accept an intent.
                self._fallback(error)
                self._apply_direct(after, kind=kind)
                return operation_key
            if operation["status"] == "complete":
                return operation_key
            try:
                self._apply_legacy(frozen, after, kind=kind)
                self._family(self._finish, operation, after_image)
            except Exception:
                # Resolve a refusal or an after-commit failure while still
                # holding the links lock. If recovery itself cannot finish,
                # suspend mirroring, retaining the intent as evidence.
                try:
                    self._family(self._recover_ready)
                except Exception as error:
                    self._fallback(error, conflict=isinstance(error, LinkConflict))
                raise
            except BaseException:
                # A process crash leaves the durable intent for next startup.
                self._started = False
                raise
            self._image = after_image
            return operation_key

    def _intent(self, connection, requested, operation_key, frozen, before, after, kind):
        connection.execute("PRAGMA synchronous=FULL")
        request_json = canonical_json({"kind": kind, "rows": requested})
        existing = connection.execute("SELECT * FROM link_operations WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            if existing["requested_json"] != request_json:
                raise LinkConflict("operation_key_reused_with_different_request")
            if existing["status"] == "pending":
                self._recover(connection)
                existing = connection.execute("SELECT * FROM link_operations WHERE operation_key=?", (operation_key,)).fetchone()
            self._require_ready(connection)
            marker = connection.execute("SELECT frozen_at FROM imports WHERE topic=?", (TOPIC,)).fetchone()
            if existing["status"] != "complete" or existing["created_at"] < marker[0]:
                raise LinkConflict("aborted_or_stale_operation_key")
            return existing
        self._require_ready(connection)
        after_digest = digest(patched(frozen.links, after))
        with sqlite_transaction(connection, immediate=True):
            operation_id = connection.execute(
                "INSERT INTO link_operations(operation_key,requested_json,before_json,after_json,before_digest,"
                "after_digest,created_at,status) VALUES (?,?,?,?,?,?,?,'pending')",
                (operation_key, request_json, canonical_json(before), canonical_json(after),
                 frozen.links_digest, after_digest, frozen.frozen_at),
            ).lastrowid
        return connection.execute("SELECT * FROM link_operations WHERE id=?", (operation_id,)).fetchone()

    def _apply_legacy(self, frozen, after, *, kind):
        # Only affected before-images need checking for our serialized write.
        # Whole-file validation belongs to startup and recovery.
        expected = {tag: frozen.links.get(tag) for tag in after}
        return self._apply_direct(after, kind=kind, expected=expected)

    def _apply_direct(self, after, *, kind, expected=None):
        with closing(sqlite3.connect(self.legacy_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)) as legacy:
            legacy.row_factory = sqlite3.Row
            legacy.execute("PRAGMA foreign_keys=ON")
            if expected is not None:
                legacy.execute("PRAGMA synchronous=FULL")
            with sqlite_transaction(legacy, immediate=True):
                if expected is not None:
                    for tag, before in expected.items():
                        found = legacy.execute("SELECT * FROM links WHERE player_tag=?", (tag,)).fetchone()
                        if (dict(found) if found else None) != before:
                            raise LinkConflict("legacy_changed_before_commit")
                if kind == "restore":
                    # The old undo deletes the selected group before restoring
                    # it. Preserve its natural row order as well as its values.
                    legacy.executemany("DELETE FROM links WHERE player_tag=?", ((tag,) for tag in after))
                for tag, row in after.items():
                    if row is None:
                        if kind != "restore":
                            legacy.execute("DELETE FROM links WHERE player_tag=?", (tag,))
                    elif kind == "refresh":
                        legacy.execute("UPDATE links SET player_name_last_seen=?,last_seen_clan_tag=?,"
                                       "last_seen_clan_code=?,last_seen_role=? WHERE player_tag=?",
                                       (*(row[key] for key in FIELDS[2:]), tag))
                    else:
                        legacy.execute("INSERT INTO links VALUES (?,?,?,?,?,?) ON CONFLICT(player_tag) DO UPDATE SET "
                                       "discord_user_id=excluded.discord_user_id,player_name_last_seen=excluded.player_name_last_seen,"
                                       "last_seen_clan_tag=excluded.last_seen_clan_tag,last_seen_clan_code=excluded.last_seen_clan_code,"
                                       "last_seen_role=excluded.last_seen_role", tuple(row[key] for key in FIELDS))
