from pathlib import Path
import unittest
from unittest.mock import patch

from tests.temp_directory import TemporaryDirectory


class FixtureDirectoryTests(unittest.TestCase):
    def test_files_are_accessible_and_context_cleanup_removes_them(self):
        with TemporaryDirectory() as name:
            path = Path(name)
            (path / "fixture").write_text("synthetic", encoding="utf-8")
            self.assertEqual((path / "fixture").read_text(encoding="utf-8"), "synthetic")
        self.assertFalse(path.exists())

    def test_failed_cleanup_is_visible_and_can_be_retried(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch("tests.temp_directory.shutil.rmtree", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                directory.cleanup()
        self.assertTrue(Path(directory.name).exists())
        directory.cleanup()
        directory.cleanup()
        self.assertFalse(Path(directory.name).exists())
