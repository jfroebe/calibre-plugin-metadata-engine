"""Verify subprocess output preserves Unicode independently of host code pages."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "run_runtime", Path(__file__).resolve().parents[1] / "ci/run_runtime.py"
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class RuntimeHarnessTests(unittest.TestCase):
    """Exercise actual child process output and failure handling."""

    def test_utf8_output_and_failures_are_preserved(self):
        """Windows code pages must not corrupt Calibre's Unicode output."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory)
            result = harness.command(
                [sys.executable, "-c", "print('日本語 · Dúné')"],
                env,
                report,
                "unicode",
            )
            self.assertEqual(result.strip(), "日本語 · Dúné")
            self.assertEqual(
                (report / "unicode.log").read_text(encoding="utf-8"), result
            )
            with self.assertRaises(RuntimeError):
                harness.command(
                    [sys.executable, "-c", "print('failure evidence'); exit(7)"],
                    env,
                    report,
                    "failure",
                )
            self.assertIn("failure evidence", (report / "failure.log").read_text())
