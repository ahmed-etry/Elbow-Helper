"""Feature-owned dataset declarations."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
import sqlite3


@dataclass(frozen=True)
class DataSource:
    alias: str
    path: Path
    tables: tuple[tuple[str, str, str], ...] = ()
    state_name: str | None = None
    level: str = "none"
    note: str = ""
    config_constant: str = ""
    coverage: Callable[[sqlite3.Connection, dict, int], dict] | None = None
    channel_keyed: bool = False
    evidence_channels: Callable[[object], set[int]] | None = None

    def resolve(self, paths):
        if self.alias == "achievements":
            return paths.data_root / "achievements" / "achievements.db"
        return self.path if self.path.is_absolute() else paths.project_root / self.path
