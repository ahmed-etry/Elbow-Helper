"""Canonical application and runtime data paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    """Resolved paths owned by the application bootstrap."""

    project_root: Path
    data_root: Path
    dotenv_file: Path
    avatar_directory: Path
    avatar_file: Path
    avatar_state_file: Path
    log_directory: Path

    @classmethod
    def from_project_root(cls, project_root: Path) -> "ApplicationPaths":
        root = project_root.resolve()
        data_root = root / "data"
        avatar_directory = data_root / ".avatar"
        return cls(
            project_root=root,
            data_root=data_root,
            dotenv_file=root / ".env",
            avatar_directory=avatar_directory,
            avatar_file=avatar_directory / "icon.gif",
            avatar_state_file=avatar_directory / "last_upload.txt",
            log_directory=data_root / ".logs",
        )

    @classmethod
    def discover(cls, start: Path | None = None) -> "ApplicationPaths":
        """Find the repository root from a path inside the application."""

        anchor = (start or Path(__file__)).resolve()
        directory = anchor if anchor.is_dir() else anchor.parent
        for candidate in (directory, *directory.parents):
            if (candidate / "pyproject.toml").is_file() or (candidate / ".git").is_dir():
                return cls.from_project_root(candidate)
        raise RuntimeError(f"Could not locate the Elbow Helper project root from {anchor}.")
