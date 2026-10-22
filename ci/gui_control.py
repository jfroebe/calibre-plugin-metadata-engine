"""Control only owned disposable Calibre container desktops over Docker exec."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import time
import uuid

from container_runtime import build_image

LABEL = "io.metadata-engine.calibre-gui"


def docker(*args: str, capture: bool = False) -> str:
    """Run a bounded Docker operation without accepting arbitrary shell text."""
    result = subprocess.run(
        ["docker", *args], check=True, timeout=60, text=True, capture_output=capture
    )
    return result.stdout if capture else ""


def owned(name: str) -> None:
    """Refuse to control containers outside this test tool's ownership label."""
    data = json.loads(docker("inspect", name, capture=True))[0]
    if data["Config"].get("Labels", {}).get(LABEL) != "true":
        raise SystemExit("Refusing to control an unrelated container")


def main() -> None:
    """Start, inspect, interact with or remove a private test desktop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("start", "windows", "click", "type", "key", "screenshot", "stop"),
    )
    parser.add_argument("--name")
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--port", type=int, default=18181)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--text")
    parser.add_argument("--key")
    args = parser.parse_args()
    host = platform.node().split(".")[0]
    if host not in {"chatgpt-dev", "chatgpt-build-02"}:
        raise SystemExit("Run on an existing authorized development worker")
    if args.action == "start":
        if not args.engine_root or not args.report:
            parser.error("start requires --engine-root and --report")
        if not 1024 <= args.port <= 65535:
            parser.error("Use an unprivileged TCP port")
        report = args.report.resolve()
        report.mkdir(parents=True, exist_ok=True)
        report.chmod(0o700)
        # The password is deliberately outside report artifacts. SSH loopback
        # forwarding is still required; no LAN or Internet listener is opened.
        password = report.parent / (report.name + "-gui-password")
        descriptor = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_urlsafe(32))
        image = build_image(args.engine_root.resolve())
        name = "calibre-gui-" + uuid.uuid4().hex[:10]
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            LABEL + "=true",
            "--shm-size",
            "1g",
            "--pids-limit",
            "512",
            "--publish",
            f"127.0.0.1:{args.port}:8181",
            "--tmpfs",
            "/config:rw,nosuid,nodev,mode=1777",
            "--env",
            f"PUID={os.getuid()}",
            "--env",
            f"PGID={os.getgid()}",
            "--env",
            "PIXELFLUX_WAYLAND=false",
            "--env",
            "CUSTOM_USER=calibre-test",
            "--env",
            "FILE__PASSWORD=/run/secrets/gui-password",
            "--mount",
            f"type=bind,src={password},dst=/run/secrets/gui-password,readonly",
            "--mount",
            f"type=bind,src={report},dst=/artifacts",
            image,
        )
        session = {
            "container": name,
            "worker": host,
            "url": f"https://127.0.0.1:{args.port}",
            "username": "calibre-test",
            "password_file": str(password),
            "image": image,
        }
        report.joinpath("session.json").write_text(
            json.dumps(session, indent=2), encoding="utf-8"
        )
        for _ in range(60):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    str(os.getuid()),
                    "--env",
                    "DISPLAY=:1",
                    name,
                    "xdotool",
                    "getdisplaygeometry",
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
                print(json.dumps(session))
                return
            time.sleep(1)
        raise SystemExit("GUI not ready; inspect this owned container logs")
    if not args.name:
        parser.error("--name is required")
    owned(args.name)
    if args.action == "stop":
        docker("rm", "-f", args.name)
        return
    prefix = ("exec", "--user", str(os.getuid()), "--env", "DISPLAY=:1", args.name)
    if args.action == "windows":
        print(
            docker(
                *prefix,
                "xdotool",
                "search",
                "--onlyvisible",
                "--name",
                ".",
                capture=True,
            )
        )
    elif args.action == "click":
        if args.x is None or args.y is None or min(args.x, args.y) < 0:
            parser.error("click requires nonnegative --x and --y")
        docker(*prefix, "xdotool", "mousemove", str(args.x), str(args.y), "click", "1")
    elif args.action == "type":
        if args.text is None:
            parser.error("type requires --text")
        docker(*prefix, "xdotool", "type", "--clearmodifiers", "--", args.text)
    elif args.action == "key":
        if not args.key or not all(ch.isalnum() or ch in "+_-" for ch in args.key):
            parser.error("key requires an X11 key combination")
        docker(*prefix, "xdotool", "key", "--clearmodifiers", args.key)
    elif args.action == "screenshot":
        docker(*prefix, "scrot", "--overwrite", "/artifacts/screenshot.png")


if __name__ == "__main__":
    main()
