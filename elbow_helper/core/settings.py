"""Runtime settings loaded at application startup."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

from dotenv import load_dotenv

from .paths import ApplicationPaths


class SettingsValidationError(RuntimeError):
    """Raised when required runtime settings are unavailable."""


def _optional_text(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Environment-backed settings needed by the application bootstrap."""

    discord_token: str | None
    coc_api_key: str | None
    openai_api_key: str | None
    google_oauth_client_id: str | None
    google_oauth_client_secret: str | None
    google_oauth_refresh_token: str | None
    google_drive_folder_id: str | None

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "RuntimeSettings":
        return cls(
            discord_token=_optional_text(values.get("DISCORD_TOKEN")),
            coc_api_key=_optional_text(values.get("COC_API_KEY")),
            openai_api_key=_optional_text(values.get("OPENAI_API_KEY")),
            google_oauth_client_id=_optional_text(
                values.get("GOOGLE_OAUTH_CLIENT_ID")
            ),
            google_oauth_client_secret=_optional_text(
                values.get("GOOGLE_OAUTH_CLIENT_SECRET")
            ),
            google_oauth_refresh_token=_optional_text(
                values.get("GOOGLE_OAUTH_REFRESH_TOKEN")
            ),
            google_drive_folder_id=_optional_text(
                values.get("GOOGLE_DRIVE_FOLDER_ID")
            ),
        )

    def require_discord_token(self) -> str:
        if self.discord_token is None:
            raise SettingsValidationError(
                "DISCORD_TOKEN is required. Add it to the project .env file or process environment."
            )
        return self.discord_token


def load_runtime_settings(paths: ApplicationPaths) -> RuntimeSettings:
    """Load the project environment file, then capture runtime settings."""

    load_dotenv(dotenv_path=paths.dotenv_file)
    return RuntimeSettings.from_mapping(os.environ)
