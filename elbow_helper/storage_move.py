"""Once-only stopped-writer reorganisation. All work runs on a worker thread.

Sources are copied before SQLite opens them, including WAL. Publishing is gated
by the workflow receipt, written last. An interrupted publish is rolled back
from its durable journal before retry; an unexpected destination is refused.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from elbow_helper.configuration.files import SQLITE_DATABASES
from elbow_helper.infrastructure.persistence.documents import Document, encode, read_document, write_document
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape

DOCUMENT_SOURCES = (
    ("clan_data/clan_activity.json", "clan_reporting"),
    ("war/war_cache.json", "wars"),
    ("cwl/cwl_threads.json", "cwl_threads"),
    ("cwl/cwl_scheduler_state.json", "cwl_reminders"),
    ("cwl/cwl_transfer_state.json", "cwl_transfers"),
    ("cwl/cwl_dashboard_state.json", "cwl_dashboards"),
    ("cwl/cwl_router_state.json", "cwl_routing"),
    ("cwl/cwl_bonus_config.json", "cwl_bonus_rules"),
    ("cwl/cwl_bonus_config.backup.json", "cwl_bonus_previous"),
    ("cwl/cwl_bonus_config_audit.json", "cwl_bonus_audit"),
    ("cwl/cwl_bonus_dashboard_state.json", "cwl_bonus_boards"),
    ("event_stats/event_stats.json", "activity_schedule"),
    ("role_connections/role_connections.json", "role_connections"),
)
OLD_DATABASES = (
    "family/family.sqlite3", "events/events.sqlite3", "clan_links/links.sqlite3",
    "clan_health/settings.sqlite3", "clan_health/clan_health.db",
    "achievements/achievements.db", "rosters/rosters.sqlite3",
    "agent/agent.sqlite3", "agent/transcripts.sqlite3", "agent/research_jobs.sqlite3",
    "agent/actions.sqlite3", "ranked_stats/ranked.sqlite3", "records/records.sqlite3",
)


def _stream_hash(handle):
    digest = sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _hash(path):
    with path.open("rb") as handle:
        return _stream_hash(handle)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _tables(connection):
    return [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def table_counts(connection):
    return {name: connection.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
            for name in _tables(connection)}


def _rows_digest(connection, table):
    # A multiset digest retains duplicate rows, independent of physical order.
    count = total = 0
    for row in connection.execute(f"SELECT * FROM {_quote(table)}"):
        encoded = json.dumps(tuple(row), ensure_ascii=False, default=lambda value: value.hex(),
                             separators=(",", ":")).encode("utf-8")
        total = (total + int.from_bytes(sha256(encoded).digest(), "big")) % (1 << 256)
        count += 1
    return count, f"{total:064x}"


def _check(connection):
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise sqlite3.DatabaseError("Store copy failed its integrity check")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise sqlite3.IntegrityError("Store copy has broken references")


def _snapshot(source, destination):
    """Never let SQLite open the original, even a read-only WAL connection."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.parent / ("source-" + uuid4().hex)
    scratch.mkdir()
    try:
        copied = scratch / source.name
        shutil.copy2(source, copied)
        wal = Path(str(source) + "-wal")
        if wal.is_file():
            shutil.copy2(wal, Path(str(copied) + "-wal"))
        with closing(sqlite3.connect(copied)) as reader, closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
            writer.execute("PRAGMA journal_mode=DELETE")
            _check(writer)
    finally:
        _remove_directory(scratch, destination.parent)


def _upgrade(path, migrations, version, schema):
    with closing(sqlite3.connect(path)) as connection:
        run_sqlite_migrations(connection, migrations, target_version=version)
        validate_sqlite_shape(connection, schema)
        _check(connection)


def _copy_tables(source, destination, tables):
    """Copy named tables into an empty namespace, then compare every value."""
    with closing(sqlite3.connect(source)) as reader, closing(sqlite3.connect(destination)) as writer, writer:
        for table in tables:
            name = _quote(table)
            source_columns = [r[1] for r in reader.execute(f"PRAGMA table_info({name})")]
            target_columns = [r[1] for r in writer.execute(f"PRAGMA table_info({name})")]
            if not source_columns or source_columns != target_columns:
                raise ValueError(f"Incompatible source columns in {table}")
            if writer.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]:
                raise ValueError(f"Conflicting destination rows in {table}")
            writer.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in source_columns)})",
                               reader.execute(f"SELECT * FROM {name}"))
            if _rows_digest(reader, table) != _rows_digest(writer, table):
                raise ValueError(f"Store copy differs in {table}")
        # Preserve AUTOINCREMENT high-water marks, including deleted last rows.
        if reader.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            for table, sequence in reader.execute("SELECT name,seq FROM sqlite_sequence"):
                if table in tables:
                    writer.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                    writer.execute("INSERT INTO sqlite_sequence VALUES (?,?)", (table, sequence))
        _check(writer)


def _verify_original_tables(source, destination):
    with closing(sqlite3.connect(source)) as reader, closing(sqlite3.connect(destination)) as writer:
        for table in _tables(reader):
            if _rows_digest(reader, table) != _rows_digest(writer, table):
                raise ValueError(f"Store move changed {table}")


def _archive(root, stage, files):
    archive = stage / "backups/before-reorganisation.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest = {}
    dbs = {path for path in files if path.suffix in (".db", ".sqlite3")}
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=1) as output:
        for relative in files:
            path = root / relative
            original = _hash(path)
            manifest[relative.as_posix()] = {"original_sha256": original, "bytes": path.stat().st_size}
            if any(str(relative) == str(db) + suffix for db in dbs for suffix in ("-wal", "-shm")):
                continue
            if relative in dbs:
                temporary = stage / "archive-copy.sqlite3"
                _snapshot(path, temporary)
                with closing(sqlite3.connect(temporary)) as connection:
                    manifest[relative.as_posix()]["tables"] = table_counts(connection)
                    manifest[relative.as_posix()]["schema"] = connection.execute("PRAGMA user_version").fetchone()[0]
                manifest[relative.as_posix()]["archived_sha256"] = _hash(temporary)
                output.write(temporary, relative.as_posix())
                temporary.unlink()
            else:
                output.write(path, relative.as_posix())
                manifest[relative.as_posix()]["archived_sha256"] = original
        output.writestr("store-move-manifest.json", encode(manifest))
    with ZipFile(archive) as check:
        for name, item in manifest.items():
            if "archived_sha256" in item:
                with check.open(name) as handle:
                    if _stream_hash(handle) != item["archived_sha256"]:
                        raise ValueError(f"Archive differs at {name}")
    return manifest


def _prepare(root, stage, sources):
    from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION, create_schema
    from elbow_helper.data.events.schema import MIGRATIONS as events_migrations
    from elbow_helper.data.events.schema import SCHEMA_VERSION as events_version, create_schema as events_schema
    from elbow_helper.features.achievements.schema import DATABASE as economy
    from elbow_helper.features.rosters.repository.migrations import initialize_roster_schema
    from elbow_helper.agent.storage import initialize_agent

    counts = {}
    for old in OLD_DATABASES:
        source = root / old
        if source.is_file():
            copied = sources / old
            _snapshot(source, copied)
            with closing(sqlite3.connect(copied)) as connection:
                counts[old] = {"schema": connection.execute("PRAGMA user_version").fetchone()[0],
                               "tables": table_counts(connection)}
    family_source = sources / "family/family.sqlite3"
    if not family_source.is_file() and any((sources / old).is_file() for old in (
            "events/events.sqlite3", "clan_health/clan_health.db", "ranked_stats/ranked.sqlite3")):
        raise ValueError("A fresh post-import family and workflow backup is required")
    if family_source.is_file():
        # This move does not repair pre-import snapshots.
        with closing(sqlite3.connect(family_source)) as connection:
            receipts = {r[0] for r in connection.execute("SELECT topic FROM imports")}
        required = {"people_membership", "wars_regular", "profile_history", "health_history", "lifecycle_last_seen", "ranked_battles", "wars_cwl"}
        if not required <= receipts or not (sources / "events/events.sqlite3").is_file():
            raise ValueError("A fresh post-import family and workflow backup is required")
        shutil.copy2(family_source, stage / "family.sqlite3")
    _upgrade(stage / "family.sqlite3", MIGRATIONS, SCHEMA_VERSION, create_schema)
    if family_source.is_file():
        _verify_original_tables(family_source, stage / "family.sqlite3")
    links = sources / "clan_links/links.sqlite3"
    if links.is_file():
        with closing(sqlite3.connect(links)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported account review source version")
        # The family database is the only source of ownership; the old links table is stale.
        _copy_tables(links, stage / "family.sqlite3", ("suggestions", "ignored_tags"))
    events = sources / "events/events.sqlite3"
    if events.is_file():
        shutil.copy2(events, stage / "workflows.sqlite3")
        with closing(sqlite3.connect(events)) as connection:
            workflows = {r[0] for r in connection.execute("SELECT workflow FROM workflow_state")}
        if not {"support", "hibernation", "transfers", "lifecycle", "recruitment", "examination"} <= workflows:
            raise ValueError("Saved workflows are missing their import receipts")
    _upgrade(stage / "workflows.sqlite3", events_migrations, events_version, events_schema)
    if events.is_file():
        _verify_original_tables(events, stage / "workflows.sqlite3")
    health = sources / "clan_health/settings.sqlite3"
    if health.is_file():
        with closing(sqlite3.connect(health)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported health settings source version")
        _copy_tables(health, stage / "workflows.sqlite3",
                     ("clan_player_health_config", "clan_health_config_audit", "old_file_copy"))
    elif (root / "clan_health/clan_health.db").is_file():
        raise ValueError("The completed health settings copy is required")
    for old, name in DOCUMENT_SOURCES:
        source = root / old
        if source.is_file():
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"Expected an object in {old}")
            document = Document(name, stage / "workflows.sqlite3")
            write_document(document, payload)
            if read_document(document) != payload:
                raise ValueError(f"Document copy differs at {old}")
            counts[old] = {"keys": len(payload), "sha256": _hash(source)}
    for old, new in (("achievements/achievements.db", "economy.sqlite3"),
                     ("rosters/rosters.sqlite3", "rosters.sqlite3")):
        source = sources / old
        if source.is_file():
            shutil.copy2(source, stage / new)
        if new == "economy.sqlite3":
            # Staging paths can be retried in the same process after a failure;
            # never use a version gate's cache for a disposable destination.
            with closing(sqlite3.connect(stage / new)) as connection:
                applied = run_sqlite_migrations(connection, economy.migrations,
                    target_version=economy.target_version, adopt_unversioned=economy.adopt)
                economy.validate(connection)
                if applied and economy.after_migrate:
                    economy.after_migrate(connection)
                _check(connection)
        else:
            with closing(sqlite3.connect(stage / new)) as connection:
                initialize_roster_schema(connection)
        if source.is_file():
            _verify_original_tables(source, stage / new)
    with closing(sqlite3.connect(stage / "agent.sqlite3")) as connection:
        initialize_agent(connection)
    # Each source uses its own former chain, validated on the disposable copy.
    from elbow_helper.agent.storage import migrations
    steps = migrations()
    for old, selected in (("agent/agent.sqlite3", steps[:1]),
                          ("agent/transcripts.sqlite3", steps[1:4]),
                          ("agent/research_jobs.sqlite3", steps[4:6]),
                          ("agent/actions.sqlite3", steps[6:])):
        source = sources / old
        if not source.is_file():
            continue
        from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
        local = tuple(SQLiteMigration(i, step.name, step.apply) for i, step in enumerate(selected, 1))
        def schema(connection, selected=selected):
            for step in selected:
                step.apply(connection)
        _upgrade(source, local, len(local), schema)
        with closing(sqlite3.connect(source)) as connection:
            tables = _tables(connection)
        _copy_tables(source, stage / "agent.sqlite3", tables)
    for old, new in (("clan_health/player_health_config.json", "settings/health-defaults.json"),
                     (".avatar/icon.gif", "appearance/avatar.gif")):
        if (root / old).is_file():
            target = stage / new
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / old, target)
    avatar_state = root / ".avatar/last_upload.txt"
    if avatar_state.is_file():
        value = float(avatar_state.read_text(encoding="utf-8").strip())
        write_document(Document("application", stage / "workflows.sqlite3"), {"avatar_uploaded_mtime": value})
    for old, new in ((".logs", "logs"), (".exports", "exports")):
        if (root / old).is_dir():
            shutil.copytree(root / old, stage / new)
    for relative in SQLITE_DATABASES:
        with closing(sqlite3.connect(stage / relative)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
            _check(connection)
            counts[str(relative)] = {"tables": table_counts(connection),
                                     "schema": connection.execute("PRAGMA user_version").fetchone()[0]}
    return counts


def _remove_directory(path, owner):
    resolved = path.resolve()
    if resolved == owner.resolve() or not resolved.is_relative_to(owner.resolve()):
        raise ValueError("Store move cleanup escaped its owned folder")
    shutil.rmtree(path)


def _rollback(root, stage):
    journal = stage / "publish.json"
    if journal.is_file():
        plan = json.loads(journal.read_text(encoding="utf-8"))
        for relative, digest in plan.items():
            path = root / relative
            allowed = (Path(relative) in SQLITE_DATABASES or relative in
                       ("settings/health-defaults.json", "appearance/avatar.gif", "backups/before-reorganisation.zip")
                       or (Path(relative).parts[0] in ("logs", "exports") and ".." not in Path(relative).parts))
            if not allowed or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Interrupted move journal escaped the data folder")
            if path.is_file():
                if _hash(path) != digest:
                    raise ValueError(f"Interrupted move destination changed: {relative}")
                path.unlink()
    _remove_directory(stage, root)


def prepare_storage(data_root):
    """Reorganise once before logging, extension loads, or background writers."""
    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stage = root / ".store-move-stage"
    receipt = root / "workflows.sqlite3"
    if receipt.is_file():
        with closing(sqlite3.connect(receipt.as_uri() + "?mode=ro", uri=True)) as connection:
            try:
                row = connection.execute("SELECT version,details_json FROM storage_move_receipt WHERE id=1").fetchone()
            except sqlite3.OperationalError:
                row = None
        if row:
            if row[0] != 1:
                raise ValueError("Unsupported store move receipt")
            json.loads(row[1])
            if any(not (root / name).is_file() for name in SQLITE_DATABASES):
                raise FileNotFoundError("A reorganised file is missing")
            # Publication committed before a crash can interrupt scratch cleanup.
            # Never roll back the now-active generation on the next restart.
            if stage.exists():
                _remove_directory(stage, root)
            return False
    if stage.exists():
        _rollback(root, stage)
    if any((root / relative).exists() for relative in SQLITE_DATABASES):
        raise FileExistsError("A store move destination already exists")
    stage.mkdir()
    sources = stage / "sources"
    sources.mkdir()
    files = sorted(p.relative_to(root) for p in root.rglob("*")
                   if p.is_file() and stage not in p.parents)
    try:
        before = {path.as_posix(): _hash(root / path) for path in files}
        counts = _prepare(root, stage, sources)
        manifest = _archive(root, stage, files)
        if any(_hash(root / path) != before[path.as_posix()] for path in files):
            raise ValueError("A source changed during the stopped-writer copy")
        targets = [p.relative_to(stage) for p in stage.rglob("*") if p.is_file()
                   and sources not in p.parents and p.suffix not in ("-wal", "-shm")
                   and not str(p).endswith(("-wal", "-shm"))]
        targets.remove(Path("workflows.sqlite3"))
        targets.sort()
        targets.append(Path("workflows.sqlite3"))
        if any((root / relative).exists() for relative in targets):
            raise FileExistsError("A store move destination already exists")
        details = {"files": manifest, "counts": counts, "published": [p.as_posix() for p in targets]}
        with closing(sqlite3.connect(stage / "workflows.sqlite3")) as connection, connection:
            connection.execute("INSERT INTO storage_move_receipt VALUES (1,1,?)", (encode(details),))
        for relative in targets:
            with (stage / relative).open("r+b") as handle:
                os.fsync(handle.fileno())
        plan = {relative.as_posix(): _hash(stage / relative) for relative in targets}
        journal = stage / "publish.tmp"
        with journal.open("w", encoding="utf-8") as handle:
            handle.write(encode(plan))
            handle.flush()
            os.fsync(handle.fileno())
        journal.replace(stage / "publish.json")
        for folder in ("settings", "appearance", "logs", "exports"):
            (root / folder).mkdir(exist_ok=True)
        for relative in targets:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage / relative).replace(target)
    except BaseException:
        _rollback(root, stage)
        raise
    _remove_directory(stage, root)
    return True
