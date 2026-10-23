"""Download and verify the pinned official Calibre build once per cache."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Install a verified archive into the explicitly provided cache directory."""
    destination = Path(sys.argv[1]).resolve()
    pin = json.loads((ROOT / "ci/compatibility.json").read_text())["calibre"]
    kind = "windows" if os.name == "nt" else "linux"
    suffix = "msi" if os.name == "nt" else "txz"
    archive = destination.parent / f"calibre-{pin['version']}.{suffix}"
    destination.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        with urlopen(pin[kind + "_url"], timeout=60) as response:
            temporary = archive.with_suffix(".download")
            with temporary.open("wb") as output:
                while data := response.read(1024 * 1024):
                    output.write(data)
            temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin[kind + "_sha256"]:
        raise ValueError("Calibre archive digest mismatch; refusing execution")
    if os.name == "nt":
        # Administrative extraction installs no services and needs no elevation.
        result = subprocess.run(
            [
                "msiexec",
                "/a",
                str(archive),
                "/qn",
                "/norestart",
                f"TARGETDIR={destination}",
                "/L*v",
                str(destination / "extract.log"),
            ],
            check=False,
            timeout=300,
        )
        if result.returncode not in (0, 3010):
            raise RuntimeError(f"MSI extraction failed: {result.returncode}")
        binaries = list(destination.rglob("calibre-debug.exe"))
        if len(binaries) != 1:
            raise RuntimeError("Expected one extracted Calibre installation")
        print(binaries[0].parent)
        return
    # Only a digest-verified official archive is extracted; tar preserves links
    # required by Calibre's relocatable distribution.
    subprocess.run(["tar", "-xJf", str(archive), "-C", str(destination)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
