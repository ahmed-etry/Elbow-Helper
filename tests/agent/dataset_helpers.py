"""Create every queryable schema using the feature's normal initializer."""
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from elbow_helper.agent.tool_support.sql.catalogue import SOURCES
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.clan_health.database import DATABASE as HEALTH_SETTINGS
from elbow_helper.features.rosters.repository.repository import RosterRepository
from elbow_helper.features.achievements.database import AchievementsDatabaseMixin
from elbow_helper.data.store.database import DATABASE as FAMILY
from elbow_helper.infrastructure.persistence.documents import Document, write_document


def synthetic_datasets(root, *, only=None):
    paths = SimpleNamespace(project_root=Path(root), data_root=Path(root) / "data")
    files = {source.alias:source.resolve(paths) for source in SOURCES
             if source.tables and (only is None or source.alias in only)}
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    if "family" in files:
        FAMILY.initialize(files["family"])
    if "links" in files:
        with patch("elbow_helper.features.account_links.database.DB_PATH", files["links"]):
            AccountLinksDbMixin()._init_db()
    if "health" in files:
        HEALTH_SETTINGS.initialize(files["health"])
    if "rosters" in files:
        RosterRepository(files["rosters"])
    if "achievements" in files:
        achievements = AchievementsDatabaseMixin()
        achievements.db_path = files["achievements"]
        achievements.logger = logging.getLogger(__name__)
        achievements.init_database()
    for source in SOURCES:
        if source.state_name and (only is None or source.alias in only):
            path = source.resolve(paths)
            if isinstance(path, Document):
                HEALTH_SETTINGS.initialize(path.database)
                write_document(path, {})
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
    if "family" in files:
        seed_record_links(paths, [])
    return paths


def seed_record_links(paths, rows):
    """Populate synthetic ownership in the sole record, never the old table."""
    from elbow_helper.configuration.files import FAMILY_DATABASE
    from elbow_helper.data.store.link_state import apply_rows, display_rows, receipt
    from elbow_helper.data.store.link_state import TOPIC, FIELDS, digest
    from elbow_helper.data.store.projections import canonical_json
    with closing(sqlite3.connect(paths.data_root / FAMILY_DATABASE)) as connection, connection:
        connection.row_factory = sqlite3.Row
        last = connection.execute("SELECT MAX(last_observed_at) FROM account_links").fetchone()[0]
        at = (last or 0) + 1
        patch_rows = {tag: dict(zip(FIELDS, (tag, owner, '', '', '', ''))) for tag, owner in rows}
        item = receipt(connection, 'people_membership_import', TOPIC, at, len(rows))
        apply_rows(connection, patch_rows, at, item, basis='legacy_current')
        display_rows(connection, patch_rows)
        connection.execute("INSERT OR IGNORE INTO imports VALUES (?,?,?,?,?,?,?,?)",
                           (TOPIC, 1, digest({}), 0, digest({}), at, canonical_json({}), item))
