"""Prevent a green unrelated run from approving changed distribution bytes."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_gate", ROOT / "ci/release_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseGateTests(unittest.TestCase):
    """Bind integration approval to exact artifacts and reviewed component pins."""

    def setUp(self):
        """Construct synthetic approval without writing a real release attestation."""
        self.manifest = json.loads((ROOT / "ci/compatibility.json").read_text())
        self.manifest["qualification_status"] = "qualified"
        self.evidence = {
            "schema_version": 1,
            "qualification_status": "qualified",
            "scope": "calibre-integration",
            "repository": "jfroebe/metadata-engine-docker",
            "run_id": 123,
            "source_sha": "a" * 40,
            "workflow_sha": "b" * 40,
            "zip_sha256": "c" * 64,
            "jobs": dict.fromkeys(gate.REQUIRED, "success"),
            **{
                field: deepcopy(self.manifest[field])
                for field in (
                    "components",
                    "snapshot_evidence",
                    "calibre",
                    "desktop_validation",
                )
            },
        }

    def test_exact_approval_passes(self):
        """A complete matching integration record permits the tested artifact."""
        gate.verify(self.manifest, self.evidence, "c" * 64)

    def test_changed_zip_components_or_runtime_are_rejected(self):
        """Neither source changes nor different runtime pins inherit approval."""
        with self.assertRaises(ValueError):
            gate.verify(self.manifest, self.evidence, "d" * 64)
        for field in (
            "components",
            "snapshot_evidence",
            "calibre",
            "desktop_validation",
        ):
            wrong = deepcopy(self.evidence)
            wrong[field] = {}
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.verify(self.manifest, wrong, "c" * 64)

    def test_missing_skipped_cancelled_or_failed_jobs_are_rejected(self):
        """Every required integration result must be present and successful."""
        for job in gate.REQUIRED:
            for outcome in (None, "skipped", "cancelled", "failure"):
                wrong = deepcopy(self.evidence)
                if outcome is None:
                    del wrong["jobs"][job]
                else:
                    wrong["jobs"][job] = outcome
                with (
                    self.subTest(job=job, outcome=outcome),
                    self.assertRaises(ValueError),
                ):
                    gate.verify(self.manifest, wrong, "c" * 64)

    def test_unqualified_or_unrelated_evidence_is_rejected(self):
        """A status flag alone or another repository's run cannot grant approval."""
        for key, value in (
            ("qualification_status", "candidate-unqualified"),
            ("scope", "other"),
            ("repository", "other/repo"),
            ("run_id", 0),
            ("source_sha", "main"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.verify(
                    self.manifest, dict(self.evidence, **{key: value}), "c" * 64
                )
