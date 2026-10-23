"""Bind release integration approval to the exact tested ZIP and component pins."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"package", "native-runtime", "engine-proxy", "linux-runtime"}


def verify(manifest: dict, evidence: dict, digest: str) -> None:
    """Reject stale artifacts, changed components and incomplete integration runs."""
    if (
        manifest.get("qualification_status") != "qualified"
        or evidence.get("qualification_status") != "qualified"
        or evidence.get("scope") != "calibre-integration"
        or evidence.get("schema_version") != 1
    ):
        raise ValueError("Integration qualification has not been recorded")
    if (
        evidence.get("repository") != "jfroebe/metadata-engine-docker"
        or not isinstance(evidence.get("run_id"), int)
        or evidence["run_id"] <= 0
        or not re.fullmatch("[0-9a-f]{40}", evidence.get("source_sha", ""))
        or not re.fullmatch("[0-9a-f]{40}", evidence.get("workflow_sha", ""))
    ):
        raise ValueError("Immutable integration workflow/source evidence required")
    if not re.fullmatch("[0-9a-f]{64}", digest) or evidence.get("zip_sha256") != digest:
        raise ValueError("Distribution differs from the exact qualified ZIP")
    for field in ("components", "snapshot_evidence", "calibre", "desktop_validation"):
        if evidence.get(field) != manifest.get(field):
            raise ValueError(f"Integration evidence differs from manifest: {field}")
    jobs = evidence.get("jobs", {})
    if not REQUIRED.issubset(jobs) or any(jobs[name] != "success" for name in REQUIRED):
        raise ValueError("Every integration job must have passed")
    for desktop in ("windows11", "macos"):
        accepted = manifest.get("desktop_validation", {}).get(desktop, {})
        if (
            accepted.get("status") != "independently-validated"
            or accepted.get("required_in_test_environment") is not False
        ):
            raise ValueError(f"Independent desktop validation missing: {desktop}")


def main() -> None:
    """Validate the downloaded distribution using the reviewed run attestation."""
    from package import verify as verify_archive

    archive = ROOT / "Calibre-Metadata-Engine.zip"
    verify_archive(archive)
    path = ROOT / "ci/release-evidence.json"
    if not path.is_file():
        raise SystemExit(
            "Missing reviewed Docker integration evidence; run validate-calibre.yml"
        )
    evidence = json.loads(path.read_text())
    manifest = json.loads((ROOT / "ci/compatibility.json").read_text())
    verify(manifest, evidence, hashlib.sha256(archive.read_bytes()).hexdigest())
    print(f"Exact tested ZIP qualified by Docker run {evidence['run_id']}")


if __name__ == "__main__":
    main()
