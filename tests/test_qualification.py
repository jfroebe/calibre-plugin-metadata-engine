"""Ensure the aggregation gate cannot turn missing validation into approval."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "qualification", Path(__file__).resolve().parents[1] / "ci/qualification.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class QualificationTests(unittest.TestCase):
    """Exercise required failures, cancellations, skipped jobs and release scope."""

    def setUp(self):
        """Use successful native tests with explicitly unrequested local jobs."""
        self.jobs = {
            name: {"result": result}
            for name, result in (
                ("package", "success"),
                ("native-runtime", "success"),
                ("worker-configuration", "skipped"),
                ("engine", "skipped"),
                ("linux-runtime", "skipped"),
            )
        }
        self.manifest = {"qualification_status": "candidate-unqualified"}

    def test_ci_success_is_not_release_qualification(self):
        """Native checks can pass while full release remains rejected."""
        gate.qualify(self.jobs, "ci", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "release", self.manifest)

    def test_every_required_result_must_be_success(self):
        """A skipped, cancelled, missing or failing native suite blocks CI."""
        for name in ("package", "native-runtime"):
            for result in ("skipped", "cancelled", "failure", None):
                with self.subTest(name=name, result=result):
                    jobs = {**self.jobs, name: {"result": result}}
                    with self.assertRaises(ValueError):
                        gate.qualify(jobs, "ci", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify({}, "ci", self.manifest)

    def test_local_mode_requires_actual_integration(self):
        """Missing local setup cannot satisfy requested local validation."""
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "local", self.manifest)
        for name in self.jobs:
            self.jobs[name] = {"result": "success"}
        gate.qualify(self.jobs, "local", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "release", self.manifest)

    def test_optional_failures_and_unknown_modes_are_rejected(self):
        """Bad optional execution or a misspelled mode cannot silently pass."""
        self.jobs["engine"] = {"result": "failure"}
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "ci", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "relese", self.manifest)
