from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import read_json_async
from elbow_helper.infrastructure.persistence import write_json_atomic
from elbow_helper.infrastructure.persistence import write_json_atomic_async


class JsonStoreTests(unittest.TestCase):
    def test_round_trip_creates_parents_and_preserves_unicode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            payload = {"member": "أحمد", "values": [1, 2, 3]}

            write_json_atomic(
                path,
                payload,
                indent=4,
                ensure_ascii=False,
            )

            self.assertEqual(read_json(path), payload)
            self.assertIn("أحمد", path.read_text(encoding="utf-8"))

    def test_missing_and_malformed_files_remain_distinguishable(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            malformed = Path(directory) / "malformed.json"
            malformed.write_text("{", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                read_json(missing)
            with self.assertRaises(json.JSONDecodeError):
                read_json(malformed)

    def test_serialization_failure_preserves_existing_file_and_cleans_temp(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomic(path, {"status": "before"})

            with self.assertRaises(TypeError):
                write_json_atomic(path, {"invalid": object()})

            self.assertEqual(read_json(path), {"status": "before"})
            self.assertEqual(list(Path(directory).glob("state.json.*.tmp")), [])

    def test_replace_failure_preserves_existing_file_and_cleans_temp(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json_atomic(path, {"status": "before"})

            with (
                patch(
                    "elbow_helper.infrastructure.persistence.json_store.os.replace",
                    side_effect=OSError("replace failed"),
                ),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                write_json_atomic(path, {"status": "after"})

            self.assertEqual(read_json(path), {"status": "before"})
            self.assertEqual(list(Path(directory).glob("state.json.*.tmp")), [])


class AsyncJsonStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_round_trip_uses_the_same_contract(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"

            await write_json_atomic_async(path, {"ready": True})

            self.assertEqual(await read_json_async(path), {"ready": True})


if __name__ == "__main__":
    unittest.main()
