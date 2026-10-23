"""Aggregate real CI outcomes without claiming unfinished release qualification."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def qualify(jobs: dict, mode: str, manifest: dict) -> None:
    """Reject failed/skipped required jobs and an unqualified release snapshot."""
    if mode not in {"ci", "local", "release"}:
        raise ValueError(f"Unknown qualification mode: {mode}")
    required = {"package", "native-runtime"}
    local = {"worker-configuration", "engine", "linux-runtime"}
    if mode in {"local", "release"}:
        required |= local
    for name in required:
        if jobs.get(name, {}).get("result") != "success":
            raise ValueError(f"Required validation did not pass: {name}")
    # Optional local checks must never hide a failure if they were requested/run.
    for name in local:
        if name in jobs and jobs[name]["result"] not in {"success", "skipped"}:
            raise ValueError(f"Local integration failed: {name}")
    if mode == "release" and manifest.get("qualification_status") != "qualified":
        raise ValueError(
            "Release blocked: compatibility snapshot remains unqualified; "
            "complete the evidence requirements in docs/testing.md"
        )


def main() -> None:
    """Fail CI on actual missing evidence and print the precise scope passed."""
    mode = os.environ.get("QUALIFICATION_MODE", "ci")
    qualify(
        json.loads(os.environ["RESULTS"]),
        mode,
        json.loads((ROOT / "ci/compatibility.json").read_text()),
    )
    print(f"Calibre {mode} validation passed")
    if mode != "release":
        print("Full release qualification is separate and remains fail-closed.")


if __name__ == "__main__":
    main()
