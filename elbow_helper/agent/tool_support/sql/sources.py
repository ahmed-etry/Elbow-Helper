"""Feature-owned dataset declarations."""

from dataclasses import dataclass
from pathlib import Path
from elbow_helper.infrastructure.persistence.documents import Document
from collections.abc import Callable
import sqlite3

from elbow_helper.configuration.files import ACHIEVEMENTS_DATABASE


@dataclass(frozen=True)
class DataSource:
    alias: str
    path: Path | Document
    tables: tuple[tuple[str, str, str], ...] = ()
    state_name: str | None = None
    level: str = "none"
    note: str = ""
    config_constant: str = ""
    coverage: Callable[[sqlite3.Connection, dict, int], dict] | None = None
    channel_keyed: bool = False
    evidence_channels: Callable[[object], set[int]] | None = None
    excluded_keys: tuple[str, ...] = ()
    snapshot: Callable | None = None

    def resolve(self, paths):
        if isinstance(self.path, Document):
            return Document(self.path.name, paths.data_root / "workflows.sqlite3")
        if self.alias == "achievements":
            return paths.data_root / ACHIEVEMENTS_DATABASE
        if self.alias == "family":
            return paths.data_root / self.path
        return self.path if self.path.is_absolute() else paths.project_root / self.path
