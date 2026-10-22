"""Prove screenshot, keyboard and click control on a disposable Linux desktop."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Start an owned desktop, interact with Calibre Qt and remove it afterward."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    control = [sys.executable, str(ROOT / "ci/gui_control.py")]
    name = None
    password_file = None
    try:
        subprocess.run(
            [*control, "start", "--report", str(report)], check=True, timeout=120
        )
        session = json.loads(report.joinpath("session.json").read_text())
        name = session["container"]
        password_file = Path(session["password_file"])
        token = "gui-proof-" + uuid.uuid4().hex[:12]
        subprocess.run(
            [
                "docker",
                "cp",
                str(ROOT / "tests/gui_probe.py"),
                name + ":/tmp/gui_probe.py",
            ],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                "--user",
                str(os.getuid()),
                "--env",
                "DISPLAY=:1",
                "--env",
                "QT_QPA_PLATFORM=xcb",
                "--env",
                "CALIBRE_GUI_PROOF_TOKEN=" + token,
                name,
                "/opt/calibre/calibre-debug",
                "-e",
                "/tmp/gui_probe.py",
            ],
            check=True,
            timeout=30,
        )
        prefix = [
            "docker",
            "exec",
            "--user",
            str(os.getuid()),
            "--env",
            "DISPLAY=:1",
            name,
            "xdotool",
        ]
        for _ in range(60):
            found = subprocess.run(
                [
                    *prefix,
                    "search",
                    "--onlyvisible",
                    "--name",
                    "^Metadata Calibre GUI control proof$",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if found.returncode == 0 and found.stdout.strip():
                window_id = found.stdout.strip().splitlines()[0]
                break
            time.sleep(1)
        else:
            raise TimeoutError("Calibre Qt control window did not appear")
        subprocess.run(
            [*prefix, "windowactivate", "--sync", window_id], check=True, timeout=20
        )
        subprocess.run(
            [*control, "type", "--name", name, "--text", token], check=True, timeout=20
        )
        # Use the actual displayed control position, allowing window-manager
        # decorations and display scaling to vary across the two workers.
        target = report / "target.json"
        for _ in range(40):
            if target.is_file():
                break
            time.sleep(0.25)
        position = json.loads(target.read_text())
        x, y = position["x"], position["y"]
        subprocess.run(
            [*control, "click", "--name", name, "--x", str(x), "--y", str(y)],
            check=True,
            timeout=20,
        )
        receipt = report / "input-proof.json"
        for _ in range(20):
            if receipt.is_file():
                break
            time.sleep(0.25)
        if not receipt.is_file():
            # Keep the screenshot to diagnose layout/focus differences.
            raise AssertionError("GUI button click did not produce a receipt")
        result = json.loads(receipt.read_text())
        if result != {"expected": token, "actual": token, "passed": True}:
            raise AssertionError("GUI keyboard input did not match the marker")
        subprocess.run(
            [*control, "key", "--name", name, "--key", "Tab"], check=True, timeout=20
        )
        subprocess.run([*control, "screenshot", "--name", name], check=True, timeout=30)
        screenshot = report / "screenshot.png"
        if (
            not screenshot.is_file()
            or screenshot.stat().st_size < 100
            or not screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise AssertionError("GUI screenshot is missing or invalid")
        print("GUI_CONTROL_PASS")
    finally:
        if name:
            subprocess.run(
                [*control, "screenshot", "--name", name], check=False, timeout=30
            )
            subprocess.run([*control, "stop", "--name", name], check=False, timeout=30)
        if password_file:
            password_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
