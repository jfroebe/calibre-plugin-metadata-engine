"""Run the existing real-Calibre harness in a pinned disposable container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (
    "ghcr.io/linuxserver/calibre@sha256:"
    "12013dbe3328eb1b1d72fcbab5ce6d321df68272dfd775082b996d8f54a37e0c"
)


def build_image(engine_root: Path) -> str:
    """Cache a local test image by immutable base and dependency input bytes."""
    dockerfile = ROOT / "ci/Dockerfile.calibre-test"
    requirements = engine_root / "requirements.txt"
    digest = hashlib.sha256(
        dockerfile.read_bytes() + requirements.read_bytes()
    ).hexdigest()[:20]
    image = "metadata-calibre-test:" + digest
    exists = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if exists.returncode == 0:
        return image
    with tempfile.TemporaryDirectory(prefix="calibre-image-") as directory:
        context = Path(directory)
        shutil.copyfile(dockerfile, context / "Dockerfile")
        shutil.copyfile(requirements, context / "engine-requirements.txt")
        args = ["docker", "build", "--tag", image]
        for variable in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if os.environ.get(variable):
                args.extend(["--build-arg", variable])
        subprocess.run([*args, str(context)], check=True, timeout=900)
    return image


def main() -> None:
    """Mount only test inputs and reports; use no network or host GUI socket."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--previous-zip", type=Path, required=True)
    args = parser.parse_args()
    if platform.node().split(".")[0] not in {"chatgpt-dev", "chatgpt-build-02"}:
        raise SystemExit("Use an existing authorized local test worker")
    engine = args.engine_root.resolve()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    image = build_image(engine)
    name = "calibre-test-" + uuid.uuid4().hex[:12]
    source_sha = os.environ.get(
        "CALIBRE_SOURCE_SHA", os.environ.get("GITHUB_SHA", "local")
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--shm-size",
        "1g",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777",
        "--tmpfs",
        "/config:rw,nosuid,nodev,mode=1777",
        "--mount",
        f"type=bind,src={ROOT},dst=/work/repo,readonly",
        "--mount",
        f"type=bind,src={engine},dst=/work/engine,readonly",
        "--mount",
        f"type=bind,src={report},dst=/reports",
        "--mount",
        f"type=bind,src={args.previous_zip.resolve()},dst=/work/previous.zip,readonly",
        "--env",
        "QT_QPA_PLATFORM=offscreen",
        "--env",
        f"RUNNER_NAME={platform.node()}",
        "--env",
        f"GITHUB_SHA={source_sha}",
        "--entrypoint",
        "/opt/calibre-test-python/bin/python",
        image,
        "/work/repo/ci/run_runtime.py",
        "--calibre-bin",
        "/opt/calibre",
        "--zip",
        "/work/repo/Calibre-Metadata-Engine.zip",
        "--previous-zip",
        "/work/previous.zip",
        "--engine-root",
        "/work/engine",
        "--report",
        "/reports",
    ]
    try:
        subprocess.run(command, check=True, timeout=900)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30
        )
        report.joinpath("container.json").write_text(
            json.dumps(
                {
                    "base_image": BASE,
                    "derived_image": image,
                    "network": "none",
                    "worker": platform.node(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
