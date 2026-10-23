"""Ensure missing qualification outcomes cannot be converted into approval."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "qualification", Path(__file__).resolve().parents[1] / "ci/qualification.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class QualificationTests(unittest.TestCase):
    """Exercise missing outcomes and the separation of native CI and distribution."""

    def setUp(self):
        """Represent native success without recorded integration approval."""
        self.jobs = {
            "package": {"result": "success"},
            "native-runtime": {"result": "success"},
            "integration-evidence": {"result": "skipped"},
        }
        self.manifest = {"qualification_status": "candidate-unqualified"}

    def test_ci_success_is_not_distribution_qualification(self):
        """Native checks can pass while exact-artifact approval remains absent."""
        gate.qualify(self.jobs, "ci", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "release", self.manifest)

    def test_every_required_result_must_be_success(self):
        """Skipped, cancelled, missing and failing native suites block CI."""
        for name in ("package", "native-runtime"):
            for result in ("skipped", "cancelled", "failure", None):
                with (
                    self.subTest(name=name, result=result),
                    self.assertRaises(ValueError),
                ):
                    gate.qualify(
                        {**self.jobs, name: {"result": result}}, "ci", self.manifest
                    )
        with self.assertRaises(ValueError):
            gate.qualify({}, "ci", self.manifest)

    def test_release_requires_successful_evidence_and_qualified_manifest(self):
        """Both artifact verification and reviewed integration status are required."""
        self.jobs["integration-evidence"] = {"result": "success"}
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "release", self.manifest)
        self.manifest["qualification_status"] = "qualified"
        gate.qualify(self.jobs, "release", self.manifest)

    def test_failures_and_unknown_modes_are_rejected(self):
        """An unexpected evidence failure or misspelled mode cannot silently pass."""
        self.jobs["integration-evidence"] = {"result": "failure"}
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "ci", self.manifest)
        with self.assertRaises(ValueError):
            gate.qualify(self.jobs, "local", self.manifest)
