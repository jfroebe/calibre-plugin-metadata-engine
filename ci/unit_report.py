"""Run host-only unittest suites without importing the Calibre package root."""

from __future__ import annotations

import argparse
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class Result(unittest.TextTestResult):
    """Record per-test results in a portable JUnit artifact."""

    def startTest(self, test):  # noqa: N802 - unittest callback contract
        """Create one XML case for each real test invocation."""
        super().startTest(test)
        self.current = ET.SubElement(self.xml, "testcase", name=test.id())

    def addFailure(self, test, err):  # noqa: N802 - unittest callback contract
        """Preserve assertion failures without converting them into success."""
        super().addFailure(test, err)
        ET.SubElement(self.current, "failure").text = self._exc_info_to_string(
            err, test
        )

    def addError(self, test, err):  # noqa: N802 - unittest callback contract
        """Preserve unexpected exceptions for CI diagnosis."""
        super().addError(test, err)
        ET.SubElement(self.current, "error").text = self._exc_info_to_string(err, test)

    def addSkip(self, test, reason):  # noqa: N802 - unittest callback contract
        """Report missing optional environment requirements as skipped."""
        super().addSkip(test, reason)
        ET.SubElement(self.current, "skipped", message=reason)


def main() -> None:
    """Run a named suite and write JUnit even when assertions fail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--require-no-skips", action="store_true")
    args = parser.parse_args()
    Result.xml = ET.Element("testsuite", name="calibre-host-validation")
    suite = unittest.defaultTestLoader.discover("tests", pattern=args.pattern)
    result = unittest.TextTestRunner(resultclass=Result, verbosity=2).run(suite)
    Result.xml.set("tests", str(result.testsRun))
    Result.xml.set("failures", str(len(result.failures)))
    Result.xml.set("errors", str(len(result.errors)))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(Result.xml).write(
        args.report, encoding="utf-8", xml_declaration=True
    )
    if (
        not result.wasSuccessful()
        or result.testsRun == 0
        or (args.require_no_skips and result.skipped)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
