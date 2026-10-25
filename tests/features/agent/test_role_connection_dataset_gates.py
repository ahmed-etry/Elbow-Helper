"""Role rules need Lead access through every dataset query route."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest

from elbow_helper.features.agent.capabilities.role_connections.data import SOURCES
from elbow_helper.features.agent.datasets.query import run_query


class RoleConnectionDatasetGateTests(unittest.TestCase):
    def test_role_connection_state_requires_lead(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / SOURCES[0].path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"connections": []}), encoding="utf-8")
            paths = SimpleNamespace(project_root=root, data_root=root)
            for levels in (set(), {"lead_plus"}, {"core"}, {"lead"}):
                with self.subTest(levels=levels):
                    result, touched = run_query(
                        SOURCES, paths, levels,
                        "SELECT * FROM state.role_connections",
                    )
                    self.assertEqual("error" not in result, "lead" in levels)
                    if "error" not in result:
                        self.assertIn("lead", touched)
