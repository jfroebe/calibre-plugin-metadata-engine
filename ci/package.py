"""Build and verify the exact, reproducible Calibre distribution."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "__init__.py",
    "config.py",
    "plugin-import-name-metadata_engine.txt",
    "README.md",
)
OUTPUT = ROOT / "Calibre-Metadata-Engine.zip"


def build() -> Path:
    """Validate UTF-8 sources and produce a byte-reproducible ZIP."""
    for name in FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        if re.search(r"^(<{7}|={7}|>{7})( |$)", text, re.MULTILINE):
            raise ValueError(f"Merge conflict in {name}")
        if re.search(r"[ \t]+$", text, re.MULTILINE):
            raise ValueError(f"Trailing whitespace in {name}")
        if name.endswith(".py"):
            ast.parse(text, filename=name)
    tree = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    version = next(
        ast.literal_eval(n.value)
        for n in cls.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "version" for t in n.targets)
    )
    version_text = ".".join(map(str, version))
    if f"Calibre-Metadata-Engine/{version_text}" not in (
        ROOT / "__init__.py"
    ).read_text(encoding="utf-8"):
        raise ValueError("Plugin version and HTTP user agent disagree")
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (ROOT / name).read_bytes())
    verify(OUTPUT)
    return OUTPUT


def verify(path: Path) -> None:
    """Reject corrupt, unexpected, stale or incomplete distribution content."""
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC failure")
        if sorted(archive.namelist()) != sorted(FILES):
            raise ValueError("Unexpected ZIP members or missing namespace marker")
        for name in FILES:
            if archive.read(name) != (ROOT / name).read_bytes():
                raise ValueError(f"ZIP does not match checkout: {name}")


if __name__ == "__main__":
    artifact = build()
    print(
        json.dumps(
            {
                "artifact": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        )
    )
