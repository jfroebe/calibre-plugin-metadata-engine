"""Aggregate native CI and exact-artifact integration approval for distribution."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def qualify(jobs: dict, mode: str, manifest: dict) -> None:
    """Never turn skipped required tests or stale integration evidence into approval."""
    if mode not in {"ci", "release"}:
        raise ValueError(
            f"Unknown qualification mode: {mode}; run local integration in Docker repo"
        )
    required = {"package", "native-runtime"}
    if mode == "release":
        required.add("integration-evidence")
    for name in required:
        if jobs.get(name, {}).get("result") != "success":
            raise ValueError(f"Required validation did not pass: {name}")
    for name, job in jobs.items():
        if job["result"] not in {"success", "skipped"}:
            raise ValueError(f"Validation failed: {name}")
    if mode == "release" and manifest.get("qualification_status") != "qualified":
        raise ValueError("Release integration evidence has not been qualified")


def main() -> None:
    """Report precise validation scope and fail on missing mandatory outcomes."""
    mode = os.environ.get("QUALIFICATION_MODE", "ci")
    qualify(
        json.loads(os.environ["RESULTS"]),
        mode,
        json.loads((ROOT / "ci/compatibility.json").read_text()),
    )
    print(f"Calibre {mode} validation passed")
    if mode != "release":
        print("Distribution also requires exact-artifact integration evidence.")


if __name__ == "__main__":
    main()
