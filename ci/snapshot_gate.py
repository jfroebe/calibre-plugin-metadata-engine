"""Verify recorded cross-repository success against exact compatibility pins."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def api(path: str):
    """Read the current workflow repository using its scoped read-only token."""
    request = Request(
        "https://api.github.com/repos/" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def verify(manifest: dict, run: dict, components: dict) -> None:
    """Reject unrelated successful runs and any mismatched component revision."""
    evidence = manifest["snapshot_evidence"]
    if (
        run.get("id") != evidence["run_id"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_sha") != evidence["head_sha"]
        or run.get("path") != evidence["workflow_path"]
    ):
        raise ValueError("Snapshot evidence is not the exact successful workflow run")
    if manifest["components"]["docker"]["sha"] != evidence["head_sha"]:
        raise ValueError("Snapshot Docker revision differs from recorded evidence")
    for key in ("engine", "plugins", "adapters"):
        if components["components"][key] != manifest["components"][key]:
            raise ValueError(f"Snapshot component mismatch: {key}")


def main() -> None:
    """Retrieve evidence and save a minimal auditable report, never a token."""
    manifest = json.loads((ROOT / "ci/compatibility.json").read_text())
    evidence = manifest["snapshot_evidence"]
    repository = evidence["repository"]
    if repository != os.environ["GITHUB_REPOSITORY"]:
        raise ValueError("Run the integration workflow in the Docker repository")
    run = api(f"{repository}/actions/runs/{evidence['run_id']}")
    blob = api(
        f"{repository}/contents/release/components.json?ref={evidence['head_sha']}"
    )
    components = json.loads(base64.b64decode(blob["content"]))
    verify(manifest, run, components)
    report = ROOT / "reports/snapshot.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "status": "passed",
                "run_id": run["id"],
                "head_sha": run["head_sha"],
                "components": components["components"],
            },
            indent=2,
        )
    )
    print("Exact engine/plugins/adapters snapshot evidence verified")


if __name__ == "__main__":
    main()
