"""Owned test directories, confined to the checkout and removed on cleanup.

Use normal inherited directory permissions. Python's Windows-only 0700 ACL
can prevent the restricted test process from reopening its own fixture files.
"""

from pathlib import Path
import shutil
import weakref
from uuid import uuid4


class TemporaryDirectory:
    def __init__(self, suffix=None, prefix=None, dir=None):
        root = Path(dir) if dir is not None else Path(__file__).resolve().parents[1] / ".test-tmp"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{prefix or 'tmp'}{uuid4().hex}{suffix or ''}"
        path.mkdir()
        self.name = str(path.resolve())
        self._finalizer = weakref.finalize(self, shutil.rmtree, self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()

    def cleanup(self):
        if self._finalizer.alive:
            # A failed removal must stay visible to unittest's cleanup result.
            shutil.rmtree(self.name)
            self._finalizer.detach()
