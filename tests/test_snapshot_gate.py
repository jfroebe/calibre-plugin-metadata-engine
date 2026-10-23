"""Reject successful but unrelated cross-repository qualification evidence."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "snapshot_gate", Path(__file__).resolve().parents[1] / "ci/snapshot_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class SnapshotGateTests(unittest.TestCase):
    """Require an exact successful workflow run and matching component pins."""

    def test_binds_success_to_workflow_revision_and_components(self):
        """A green result alone cannot qualify a different source snapshot."""
        components = {k: {"sha": k * 40} for k in ("engine", "plugins", "adapters")}
        manifest = {
            "components": {**components, "docker": {"sha": "d" * 40}},
            "snapshot_evidence": {
                "run_id": 123,
                "head_sha": "d" * 40,
                "workflow_path": ".github/workflows/validate-snapshot.yml",
            },
        }
        run = {
            "id": 123,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "d" * 40,
            "path": manifest["snapshot_evidence"]["workflow_path"],
        }
        gate.verify(manifest, run, {"components": components})
        for key, value in (
            ("id", 124),
            ("conclusion", "failure"),
            ("status", "queued"),
            ("head_sha", "e" * 40),
            ("path", "other.yml"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.verify(
                    manifest, dict(run, **{key: value}), {"components": components}
                )
        wrong = deepcopy(components)
        wrong["engine"]["sha"] = "different"
        with self.assertRaises(ValueError):
            gate.verify(manifest, run, {"components": wrong})
