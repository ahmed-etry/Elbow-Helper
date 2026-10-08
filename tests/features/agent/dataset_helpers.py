"""Create every queryable schema using the feature's normal initializer."""
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from elbow_helper.features.agent.datasets.catalogue import SOURCES
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.clan_health.database.repository import ClanHealthRepository
from elbow_helper.features.rosters.repository.repository import RosterRepository
from elbow_helper.features.records.database.repository import RecordRepository
from elbow_helper.features.achievements.database import AchievementsDatabaseMixin


def synthetic_datasets(root):
    paths = SimpleNamespace(project_root=Path(root), data_root=Path(root) / "data")
    files = {source.alias:source.resolve(paths) for source in SOURCES if source.tables}
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    with patch("elbow_helper.features.account_links.database.DB_PATH", files["links"]):
        AccountLinksDbMixin()._init_db()
    ClanHealthRepository(files["health"]).initialize()
    RosterRepository(files["rosters"])
    RecordRepository(files["records"]).initialize()
    achievements = AchievementsDatabaseMixin()
    achievements.db_path = files["achievements"]
    achievements.logger = logging.getLogger(__name__)
    achievements.init_database()
    for source in SOURCES:
        if source.state_name:
            path = source.resolve(paths)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
    return paths
