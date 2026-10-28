"""Read-only frozen legacy snapshots; never adopt or upgrade the source file."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3

from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape

from . import now_us
from .projections import canonical_json, link_projection

FIELDS = ("player_tag", "discord_user_id", "player_name_last_seen", "last_seen_clan_tag",
          "last_seen_clan_code", "last_seen_role")
TOPIC = "people_membership"


class LinkConflict(RuntimeError):
    """Reconciliation cannot safely choose either image; stop the mirror."""


class ImportMismatch(LinkConflict):
    def __init__(self, reason, checks=None):
        self.checks = checks or {}
        super().__init__(reason)


class BootstrapRequired(ImportMismatch):
    """A complete, fresh nine-clan bootstrap is needed before importing."""


def digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def legacy_shape(connection):
    # Kept here, rather than importing the feature, so the data layer owns no
    # feature configuration and cannot accidentally migrate the rollback file.
    connection.execute("""CREATE TABLE links (
        player_tag TEXT PRIMARY KEY, discord_user_id INTEGER NOT NULL,
        player_name_last_seen TEXT DEFAULT '', last_seen_clan_tag TEXT DEFAULT '',
        last_seen_clan_code TEXT DEFAULT '', last_seen_role TEXT DEFAULT '')""")
    connection.execute("""CREATE TABLE suggestions (
        player_tag TEXT PRIMARY KEY, player_name TEXT NOT NULL, current_clan_code TEXT NOT NULL,
        current_clan_tag TEXT NOT NULL, proposed_discord_user_id INTEGER NOT NULL,
        proposed_display_name TEXT DEFAULT '', review_channel_id INTEGER DEFAULT 0,
        review_message_id INTEGER DEFAULT 0)""")
    connection.execute("CREATE TABLE ignored_tags (player_tag TEXT PRIMARY KEY)")


def validate_legacy(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    # The audited version-0 baseline has the same six-column format. Validate
    # its whole shape read-only, without stamping a version on the old file.
    if version not in (0, 1):
        raise ImportMismatch("unsupported_legacy_version")
    validate_sqlite_shape(connection, legacy_shape, warn=False)
    with closing(sqlite3.connect(":memory:")) as reference:
        legacy_shape(reference)
        for table in ("links", "suggestions", "ignored_tags"):
            wanted = [tuple(row) for row in reference.execute(f"PRAGMA table_info({table})")]
            found = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
            if found != wanted:
                raise ImportMismatch("legacy_schema_mismatch:" + table)
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ImportMismatch("legacy_integrity_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ImportMismatch("legacy_foreign_key_check")
    return version


def link_rows(connection):
    # Preserve the old facade's natural row order; canonical_json sorts keys
    # independently when computing digests.
    rows = [dict(row) for row in connection.execute("SELECT * FROM links")]
    payload = {row["player_tag"]: row for row in rows}
    if len(payload) != len(rows):
        raise ImportMismatch("duplicate_legacy_tag")
    try:
        link_projection(payload)
    except ValueError as error:
        raise ImportMismatch(str(error)) from error
    return payload


@dataclass(frozen=True)
class LegacySnapshot:
    version: int
    content_digest: str
    frozen_at: int
    links: dict
    auxiliary: dict

    @property
    def links_digest(self):
        return digest(self.links)

    @property
    def normalized(self):
        return {row["player_tag"]: row for row in link_projection(self.links)}


def snapshot(path, at=None):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        with sqlite_transaction(connection):
            version = validate_legacy(connection)
            frozen_at = now_us() if at is None else at
            if type(frozen_at) is not int or frozen_at < 0:
                raise ValueError("invalid_freeze_time")
            links = link_rows(connection)
            auxiliary = {table: [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY player_tag")] for table in ("suggestions", "ignored_tags")}
            return LegacySnapshot(version, digest({"version": version, "links": links, **auxiliary}),
                                  frozen_at, links, auxiliary)
