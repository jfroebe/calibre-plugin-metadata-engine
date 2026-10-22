"""Download and verify the pinned official Linux Calibre build once per cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Install a verified archive into the explicitly provided cache directory."""
    destination = Path(sys.argv[1]).resolve()
    pin = json.loads((ROOT / "ci/compatibility.json").read_text())["calibre"]
    archive = destination.parent / f"calibre-{pin['version']}.txz"
    destination.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        with urlopen(pin["linux_url"], timeout=60) as response:
            temporary = archive.with_suffix(".download")
            with temporary.open("wb") as output:
                while data := response.read(1024 * 1024):
                    output.write(data)
            temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["linux_sha256"]:
        raise ValueError("Calibre archive digest mismatch; refusing execution")
    # Only a digest-verified official archive is extracted; tar preserves links
    # required by Calibre's relocatable distribution.
    subprocess.run(["tar", "-xJf", str(archive), "-C", str(destination)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
