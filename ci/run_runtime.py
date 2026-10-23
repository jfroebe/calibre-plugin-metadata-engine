"""Install and exercise the tested ZIP using a real isolated Calibre runtime."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def command(args: list[str], env: dict, report: Path, name: str) -> str:
    """Run a bounded command and retain its isolated, sanitized diagnostics."""
    result = subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    report.joinpath(name + ".log").write_text(output, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{name} exited {result.returncode}; see report")
    return result.stdout


def executable(directory: Path, name: str) -> str:
    """Resolve Linux or Windows Calibre tools from the pinned installation."""
    path = directory / (name + (".exe" if os.name == "nt" else ""))
    if not path.is_file():
        raise FileNotFoundError(f"Required real Calibre executable: {path}")
    return str(path)


@contextmanager
def engine(root: Path, work: Path, report: Path):
    """Start the actual pinned engine on loopback and stop it deterministically."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    with report.joinpath("engine.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/engine_service.py"),
                "--engine-root",
                str(root),
                "--work",
                str(work),
                "--port",
                str(port),
                *(
                    ["--pg-dsn", os.environ["CALIBRE_TEST_PG_DSN"]]
                    if os.environ.get("CALIBRE_TEST_PG_DSN")
                    else []
                ),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError("Disposable engine failed; see engine.log")
                try:
                    with opener.open(url + "/health", timeout=1) as response:
                        health = json.load(response)
                        if (
                            health["ok"]
                            and health.get("cache", {}).get("ready") is True
                        ):
                            break
                except (OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("Disposable engine readiness timed out")
            yield url, opener
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    """Qualify packaging, namespace, lifecycle and a disposable book library."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibre-bin", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--previous-zip", type=Path)
    args = parser.parse_args()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    binary = args.calibre_bin.resolve()
    archive = args.zip.resolve()
    env = os.environ.copy()
    # Credentials are never inherited into third-party cover fixture tests.
    for key in list(env):
        if key.lower().endswith("_proxy") or key.startswith("ME_"):
            env.pop(key)
    # All proxy settings were removed above. A remaining NO_PROXY entry makes
    # Calibre print proxy diagnostics into otherwise machine-readable OPF/JSON.
    env.update(QT_QPA_PLATFORM="offscreen")
    metadata = {
        "os": platform.platform(),
        "host_python": platform.python_version(),
        "worker": os.environ.get("RUNNER_NAME", platform.node()),
        "plugin_commit": os.environ.get(
            "CALIBRE_SOURCE_SHA", os.environ.get("GITHUB_SHA")
        ),
        "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "proxy_mode": "local loopback bypass; external providers disabled",
        "status": "failed",
    }
    tools = {
        name: executable(binary, name)
        for name in (
            "calibre-debug",
            "calibre-customize",
            "fetch-ebook-metadata",
            "calibredb",
        )
    }
    env["CALIBRE_TEST_DEBUG"] = tools["calibre-debug"]
    try:
        with tempfile.TemporaryDirectory(prefix="calibre-validation-") as tmp:
            work = Path(tmp)
            for key, name in (
                ("CALIBRE_CONFIG_DIRECTORY", "config"),
                ("CALIBRE_CACHE_DIRECTORY", "cache"),
                ("TMPDIR", "tmp"),
                ("TEMP", "tmp"),
                ("TMP", "tmp"),
            ):
                path = work / name
                path.mkdir(exist_ok=True)
                env[key] = str(path)
            metadata["calibre"] = command(
                [tools["calibre-debug"], "--version"], env, report, "version"
            )
            metadata["bundled_python"] = command(
                [
                    tools["calibre-debug"],
                    "-c",
                    "import sys; print(sys.version)",
                ],
                env,
                report,
                "python-version",
            )
            preferences = {
                "base_url": "http://127.0.0.1:18790",
                "plugin_ids": "fixture,other",
                "max_results": 7,
                "per_plugin_limit": 3,
                "api_token": "synthetic-upgrade-token",
                "verify_ssl": False,
                "include_source_tag": True,
            }
            select_plugin = (
                "from calibre.customize.ui import metadata_plugins; "
                "p=next(p for p in metadata_plugins(('identify', 'cover')) "
                'if p.name=="Metadata Engine"); '
            )
            if args.previous_zip:
                command(
                    [
                        tools["calibre-customize"],
                        "-a",
                        str(args.previous_zip.resolve()),
                    ],
                    env,
                    report,
                    "install-previous",
                )
                command(
                    [
                        tools["calibre-debug"],
                        "-c",
                        select_plugin
                        + f"values={preferences!r}; "
                        + "[p.prefs.__setitem__(k, v) for k, v in values.items()]",
                    ],
                    env,
                    report,
                    "seed-previous-preferences",
                )
            command(
                [tools["calibre-customize"], "-a", str(archive)], env, report, "install"
            )
            if args.previous_zip:
                actual = command(
                    [
                        tools["calibre-debug"],
                        "-c",
                        select_plugin
                        + "import json; "
                        + f"keys={list(preferences)!r}; "
                        + "print(json.dumps({k:p.prefs[k] for k in keys}))",
                    ],
                    env,
                    report,
                    "upgraded-preferences",
                )
                if json.loads(actual) != preferences:
                    raise AssertionError("Upgrade lost or changed prior preferences")
                metadata["upgrade_preferences"] = "passed"

            listing = command(
                [tools["calibre-customize"], "-l"], env, report, "registration"
            )
            if "Metadata Engine" not in listing:
                raise AssertionError("Metadata Engine was not registered")
            command(
                [tools["calibre-debug"], "-e", str(ROOT / "tests/calibre_runtime.py")],
                env,
                report,
                "runtime-regressions",
            )
            if args.engine_root:
                engine_work = work / "engine"
                engine_work.mkdir()
                with engine(args.engine_root.resolve(), engine_work, report) as (
                    url,
                    opener,
                ):
                    configure = (
                        "from calibre.customize.ui import metadata_plugins; "
                        "p=next(p for p in metadata_plugins(('identify', 'cover')) "
                        'if p.name=="Metadata Engine"); '
                        f'p.prefs["base_url"]={url!r}; '
                        'p.prefs["plugin_ids"]="calibre_fixture"; '
                        'p.prefs["include_source_tag"]=True'
                    )
                    command(
                        [tools["calibre-debug"], "-c", configure],
                        env,
                        report,
                        "configure-engine",
                    )
                    opf, cover = work / "metadata.opf", work / "cover.jpg"
                    fetch = [
                        tools["fetch-ebook-metadata"],
                        "--allowed-plugin",
                        "Metadata Engine",
                        "--isbn",
                        "9780547928227",
                        "--opf",
                        "--cover",
                        str(cover),
                    ]
                    opf.write_text(
                        command(fetch, env, report, "engine-fetch"), encoding="utf-8"
                    )
                    xml = ET.parse(opf)
                    titles = xml.findall(".//{http://purl.org/dc/elements/1.1/}title")
                    if not titles or titles[0].text != "The Hobbit":
                        raise AssertionError("Wrong actual engine metadata title")
                    if not cover.is_file() or cover.stat().st_size < 20:
                        raise AssertionError("No actual engine cover downloaded")
                    with opener.open(url + "/test-provider-calls") as response:
                        calls_before = json.load(response)["calls"]
                    command(fetch, env, report, "engine-fetch-cached")
                    with opener.open(url + "/test-provider-calls") as response:
                        calls_after = json.load(response)["calls"]
                    if calls_before != calls_after or calls_before < 1:
                        raise AssertionError("Repeat lookup bypassed engine cache")
                    library = work / "library"
                    book = work / "synthetic.txt"
                    book.write_text(
                        "Synthetic disposable integration book.", encoding="utf-8"
                    )
                    command(
                        [
                            tools["calibredb"],
                            "--with-library",
                            str(library),
                            "add",
                            "--title",
                            "Unenriched fixture",
                            str(book),
                        ],
                        env,
                        report,
                        "library-add",
                    )
                    command(
                        [
                            tools["calibredb"],
                            "--with-library",
                            str(library),
                            "set_metadata",
                            "1",
                            str(opf),
                        ],
                        env,
                        report,
                        "library-metadata",
                    )
                    command(
                        [
                            tools["calibredb"],
                            "--with-library",
                            str(library),
                            "set_metadata",
                            "1",
                            "--field",
                            f"cover:{cover}",
                        ],
                        env,
                        report,
                        "library-cover",
                    )
                    actual = command(
                        [
                            tools["calibredb"],
                            "--with-library",
                            str(library),
                            "list",
                            "--for-machine",
                            "--fields",
                            "title,authors,isbn,tags",
                        ],
                        env,
                        report,
                        "library-query",
                    )
                    books = json.loads(actual)
                    if (
                        len(books) != 1
                        or books[0]["title"] != "The Hobbit"
                        or books[0]["isbn"] != "9780547928227"
                    ):
                        raise AssertionError("Library metadata differs from fixture")
                    if "metadata-engine:calibre_fixture" not in [
                        tag.casefold() for tag in books[0]["tags"]
                    ]:
                        raise AssertionError("Library source provenance missing")
                    metadata["engine_integration"] = "passed"
            command(
                [tools["calibre-customize"], "-r", "Metadata Engine"],
                env,
                report,
                "remove",
            )
            remaining = command(
                [tools["calibre-customize"], "-l"],
                env,
                report,
                "registration-after-remove",
            )
            if "Metadata Engine" in remaining:
                raise AssertionError("Plugin registration remains after removal")
            command(
                [tools["calibre-customize"], "-a", str(archive)],
                env,
                report,
                "reinstall",
            )
            metadata["status"] = "passed"
    finally:
        report.joinpath("environment.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
