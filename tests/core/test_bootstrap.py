from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.core.settings import RuntimeSettings
from elbow_helper.core.settings import SettingsValidationError


class ApplicationPathsTests(unittest.TestCase):
    def test_paths_share_one_project_and_data_root(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()

            paths = ApplicationPaths.from_project_root(root)

            self.assertEqual(paths.project_root, root)
            self.assertEqual(paths.data_root, root / "data")
            self.assertEqual(paths.dotenv_file, root / ".env")
            self.assertEqual(paths.avatar_file, root / "data" / ".avatar" / "icon.gif")
            self.assertEqual(paths.avatar_state_file, root / "data" / ".avatar" / "last_upload.txt")
            self.assertEqual(paths.log_directory, root / "data" / ".logs")

    def test_discovery_walks_up_to_the_project_marker(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            nested = root / "elbow_helper" / "core"
            nested.mkdir(parents=True)
            (root / ".git").mkdir()

            paths = ApplicationPaths.discover(nested)

            self.assertEqual(paths.project_root, root)


class RuntimeSettingsTests(unittest.TestCase):
    def test_settings_are_loaded_from_a_mapping(self) -> None:
        settings = RuntimeSettings.from_mapping(
            {
                "DISCORD_TOKEN": "  token-value  ",
                "COC_API_KEY": "  coc-token  ",
                "OPENAI_API_KEY": "  openai-token  ",
                "GOOGLE_OAUTH_CLIENT_ID": "  google-client  ",
                "GOOGLE_OAUTH_CLIENT_SECRET": "  google-secret  ",
                "GOOGLE_OAUTH_REFRESH_TOKEN": "  google-refresh  ",
                "GOOGLE_DRIVE_FOLDER_ID": "  google-folder  ",
            }
        )

        self.assertEqual(settings.discord_token, "token-value")
        self.assertEqual(settings.coc_api_key, "coc-token")
        self.assertEqual(settings.openai_api_key, "openai-token")
        self.assertEqual(settings.google_oauth_client_id, "google-client")
        self.assertEqual(settings.google_oauth_client_secret, "google-secret")
        self.assertEqual(settings.google_oauth_refresh_token, "google-refresh")
        self.assertEqual(settings.google_drive_folder_id, "google-folder")
        self.assertEqual(settings.require_discord_token(), "token-value")

    def test_missing_discord_token_fails_startup_validation(self) -> None:
        settings = RuntimeSettings.from_mapping({"DISCORD_TOKEN": "  "})

        with self.assertRaisesRegex(SettingsValidationError, "DISCORD_TOKEN is required"):
            settings.require_discord_token()
