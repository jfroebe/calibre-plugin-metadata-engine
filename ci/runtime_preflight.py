"""Report Linux prerequisites before attempting a real Calibre qualification."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
from pathlib import Path


def inspect(binary: Path) -> dict:
    """Check documented ABI requirements and diagnose launcher prerequisites."""
    libc_name, libc_version = platform.libc_ver()
    checks = {
        "architecture": platform.machine(),
        "glibc": libc_version,
        "glibc_ok": libc_name == "glibc"
        and tuple(map(int, libc_version.split("."))) >= (2, 34),
        "libraries": {},
    }
    for library in (
        "libstdc++.so.6",
        "libEGL.so.1",
        "libOpenGL.so.0",
        "libxcb-cursor.so.0",
        "libxcb-xinerama.so.0",
    ):
        try:
            ctypes.CDLL(library)
            checks["libraries"][library] = "present"
        except OSError:
            checks["libraries"][library] = "missing"
    for name, path in (
        ("proc_self_exe", "/proc/self/exe"),
        ("proc_numeric_exe", f"/proc/{os.getpid()}/exe"),
    ):
        try:
            os.readlink(path)
            checks[name] = "readable"
        except OSError as exc:
            checks[name] = type(exc).__name__
    env = dict(os.environ, LD_LIBRARY_PATH=str(binary / "lib"))
    for name, path in (
        ("launcher", binary / "lib/libcalibre-launcher.so"),
        ("qt_offscreen", binary / "plugins/platforms/libqoffscreen.so"),
        ("qt_xcb", binary / "plugins/platforms/libqxcb.so"),
    ):
        result = subprocess.run(
            ["ldd", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        checks[name + "_missing"] = [
            line.strip() for line in result.stdout.splitlines() if "not found" in line
        ]
        checks[name + "_ldd_status"] = result.returncode
    return checks


def main() -> None:
    """Write actionable diagnostics; offscreen mode does not require XCB."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibre-bin", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    checks = inspect(args.calibre_bin.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    if not checks["glibc_ok"]:
        raise SystemExit("Calibre requires GLIBC >= 2.34")
    if checks["launcher_missing"] or checks["qt_offscreen_missing"]:
        raise SystemExit("Missing Calibre/headless Qt runtime libraries")
    if checks["proc_numeric_exe"] != "readable":
        raise SystemExit(
            "Numeric /proc/PID/exe is unavailable; Calibre launcher may fail"
        )


if __name__ == "__main__":
    main()
