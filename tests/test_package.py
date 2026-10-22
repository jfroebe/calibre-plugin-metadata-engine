"""Regression checks for the distributable rather than just a checkout."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "package", Path(__file__).resolve().parents[1] / "ci/package.py"
)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackagingTests(unittest.TestCase):
    """Ensure deterministic output and fail closed on tampered archives."""

    def test_reproducible_source_exact_zip(self):
        """Repeated builds must be identical and match every source byte."""
        first = package.build().read_bytes()
        self.assertEqual(first, package.build().read_bytes())
        package.verify(package.OUTPUT)

    def test_missing_namespace_marker_rejected(self):
        """Prevent recurrence of Calibre's multi-module import failure."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in package.FILES:
                    if name.endswith(".py"):
                        archive.writestr(name, (package.ROOT / name).read_bytes())
            with self.assertRaisesRegex(ValueError, "namespace marker"):
                package.verify(path)

    def test_stale_source_rejected(self):
        """A syntactically valid ZIP with old source is not releasable."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in package.FILES:
                    archive.writestr(
                        name,
                        b"old"
                        if name == "__init__.py"
                        else (package.ROOT / name).read_bytes(),
                    )
            with self.assertRaisesRegex(ValueError, "does not match"):
                package.verify(path)
