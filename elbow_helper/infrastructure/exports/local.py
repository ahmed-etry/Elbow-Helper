"""Local export path and retention ownership."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path


class LocalExportStore:
    """Create export paths and remove expired files from one directory."""

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = 0,
    ):
        self.directory = directory
        self.retention_days = max(0, int(retention_days))

    def path_for(self, filename: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / filename

    def cleanup(self, pattern: str) -> tuple[int, str | None]:
        if self.retention_days <= 0 or not self.directory.exists():
            return 0, None
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.retention_days
        )
        deleted = 0
        try:
            for path in self.directory.glob(pattern):
                try:
                    modified = datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    )
                    if modified < cutoff:
                        path.unlink(missing_ok=True)
                        deleted += 1
                except OSError:
                    continue
        except OSError as error:
            return deleted, f"local export cleanup failed: {error}"
        return deleted, None
