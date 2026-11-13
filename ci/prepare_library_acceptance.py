"""Prepare a local acceptance sample and validate its two inputs without lookups."""

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import urlopen

ENGINE = "http://192.168.0.59:14000"
SOURCE = "//192.168.0.59/ebooks"
BASELINE_SHA = "6138342756d9406157dba63698b7157dfa11dbf5b2d2b133b1860f56ceba4f0b"


def library_mount():
    """Resolve the exact production library under its existing read-only SMB mount."""
    result = subprocess.run(
        [
            "findmnt",
            "--json",
            "--list",
            "-t",
            "cifs,smb3",
            "-o",
            "SOURCE,TARGET,OPTIONS",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    mounts = json.loads(result.stdout)["filesystems"] if result.stdout else []
    library = PurePosixPath("safe/Main Library")
    matches = set()
    for mount in mounts:
        source = mount["source"]
        share, separator, subpath = source.partition("[")
        if share.casefold() == SOURCE and separator:
            relative = subpath.removesuffix("]").lstrip("/")
        elif source.casefold() == SOURCE or source.casefold().startswith(SOURCE + "/"):
            relative = source[len(SOURCE) :].lstrip("/")
        else:
            continue
        if "ro" not in mount["options"].split(","):
            continue
        prefix = PurePosixPath(relative)
        if not library.is_relative_to(prefix):
            continue
        root = Path(mount["target"]) / library.relative_to(prefix)
        database = root / "metadata.db"
        if database.is_file() and os.access(database, os.R_OK):
            matches.add(root.resolve())
    if not matches:
        raise RuntimeError(
            "No readable metadata.db at the exact safe/Main Library path on the "
            f"read-only eBooks share among {len(mounts)} visible SMB mounts."
        )
    print(
        f"Verified {len(matches)} local path(s) to the exact library export.",
        flush=True,
    )
    return min(matches)


def snapshot(source, destination):
    """Copy a consistent SQLite snapshot through the read-only backup API."""
    if destination.exists():
        return
    temporary = destination.with_suffix(".partial")
    temporary.unlink(missing_ok=True)
    deadline = time.monotonic() + 300

    def progress(_status, _remaining, _total):
        """Bound the backup time if the source stays busy."""
        if time.monotonic() > deadline:
            raise TimeoutError("Calibre SQLite backup exceeded five minutes")

    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db,
            closing(sqlite3.connect(temporary)) as copy,
        ):
            db.execute("PRAGMA query_only = ON")
            db.backup(copy, pages=512, progress=progress, sleep=0.1)
            if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Calibre snapshot did not pass SQLite quick_check")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def verify_zip(path):
    """Check archive integrity and read the declared plugin version statically."""
    with zipfile.ZipFile(path) as archive:
        if archive.testzip():
            raise ValueError("Plugin ZIP failed integrity check")
        tree = ast.parse(archive.read("__init__.py"))
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MetadataEngineSource"
        )
        for node in plugin.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "version"
                for target in node.targets
            ):
                return ".".join(map(str, ast.literal_eval(node.value)))
    raise ValueError("Plugin ZIP is missing its version declaration")


def verify_engine():
    """Check the test engine health and discovery routes without performing searches."""
    for route in ("/health", "/plugins"):
        try:
            with urlopen(ENGINE + route, timeout=15) as response:
                payload = json.load(response)
                if not payload or not isinstance(payload, (dict, list)):
                    return False
        except (OSError, URLError, ValueError):
            return False
    return True


def main():
    """Prepare locally and expose aggregate readiness information in the job log."""
    os.umask(0o077)
    baseline, candidate = map(Path, sys.argv[1:])
    if hashlib.sha256(baseline.read_bytes()).hexdigest() != BASELINE_SHA:
        raise ValueError(
            "Published baseline ZIP digest differs from the pinned release"
        )
    versions = [verify_zip(path) for path in (baseline, candidate)]
    print(
        f"Verified baseline {versions[0]} and candidate {versions[1]} ZIPs.", flush=True
    )
    endpoint_ready = verify_engine()
    print(
        f"Test engine health and provider discovery reachable: {endpoint_ready}",
        flush=True,
    )

    root = library_mount()
    print(
        "Production Calibre source is readable on the read-only SMB mount.", flush=True
    )
    state = Path.home() / ".local/state/calibre-library-acceptance/pilot-1"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        raise RuntimeError("Pilot state directory is not private")
    database = state / "metadata.db"
    snapshot(root / "metadata.db", database)
    for source, name in ((baseline, "baseline.zip"), (candidate, "candidate.zip")):
        shutil.copyfile(source, state / name)
    runner = (
        Path.home()
        / ".local/share/calibre-library-acceptance/current/library_acceptance.py"
    )
    manifest = state / "manifest.json"
    if not manifest.is_file():
        result = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--state",
                str(state),
                "prepare",
                "--database",
                str(database),
                "--books",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if result.returncode:
            log = state / "prepare.log"
            traceback = log.read_text(errors="replace") if log.exists() else ""
            labels = (
                "ModuleNotFoundError",
                "ImportError",
                "OperationalError",
                "ValueError",
                "AttributeError",
                "PermissionError",
                "FileNotFoundError",
                "TypeError",
            )
            found = [
                label
                for label in labels
                if label in traceback or label in result.stderr
            ]
            raise RuntimeError(
                "Sample preparation failed. Local logs retained. Error classes: "
                + ", ".join(found)
            )
    frozen = json.loads(manifest.read_text())
    formats = json.loads((state / "embedded.local.json").read_text())
    status = {}
    for item in formats:
        label = item["status"]
        status[label] = status.get(label, 0) + 1
    print(
        json.dumps(
            {
                "books": frozen["counts"],
                "lookup_cases": len(frozen["cases"]),
                "embedded_formats": status,
                "lookups_started": False,
            }
        )
    )
    if not endpoint_ready:
        raise SystemExit("Sample ready, but the test engine preflight did not pass.")


if __name__ == "__main__":
    main()
