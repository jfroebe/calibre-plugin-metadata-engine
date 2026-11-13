"""Coordinate opt-in Calibre library acceptance runs on a local Meta worker."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RUNTIME = Path(__file__).resolve().with_name("library_acceptance_runtime.py")


def read(path):
    """Read a private JSON record."""
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    """Publish a complete checkpoint atomically."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    """Fingerprint a runtime input or installable ZIP."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv, directory, name, timeout, env=None):
    """Run an isolated Calibre command and retain its local diagnostic log."""
    with (directory / (name + ".log")).open("ab") as log:
        return subprocess.run(
            [str(value) for value in argv],
            cwd=directory,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        ).returncode


def calibre(args, tool):
    """Resolve an executable in the configured official Calibre installation."""
    return args.calibre_bin / tool


def freeze(args, state):
    """Create a private immutable sample from a Calibre snapshot and ebook mount."""
    if (state / "manifest.json").exists():
        raise SystemExit("Manifest already exists. Choose a new state directory.")
    request = state / "prepare.json"
    write(
        request,
        {
            "database": str(args.database),
            "books": str(args.books),
            "sample": args.sample,
            "seed": args.seed,
        },
    )
    env = profile_env(state / "prepare-profile")
    if command(
        [calibre(args, "calibre-debug"), "-e", RUNTIME, "--", "prepare", request],
        state,
        "prepare",
        3600,
        env,
    ):
        raise SystemExit("Sampling failed. See prepare.log.")
    manifest = read(state / "manifest.json")
    print(json.dumps({"books": manifest["counts"], "cases": len(manifest["cases"])}))


def profile_env(profile):
    """Isolate Calibre preferences, cache, and temporary data from host profiles."""
    profile.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in (
        "CALIBRE_CONFIG_DIRECTORY",
        "CALIBRE_CACHE_DIRECTORY",
        "CALIBRE_TEMP_DIR",
    ):
        location = profile / key.lower()
        location.mkdir(exist_ok=True)
        env[key] = str(location)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def attempt(args, state, entry, build, run, reference, replay):
    """Run one bounded, resumable attempt with a fresh installed plugin profile."""
    case = state / "cases" / entry["id"]
    root = run / entry["id"] / build
    root.mkdir(parents=True, exist_ok=True)
    attempts = sorted(root.glob("attempt-*"))
    directory = root / f"attempt-{len(attempts) + 1:04d}"
    directory.mkdir()
    env = profile_env(directory / "profile")
    env["CALIBRE_ACCEPTANCE_TOKEN"] = os.getenv(
        f"CALIBRE_ACCEPTANCE_{build.upper()}_TOKEN", ""
    )
    request = directory / "request.json"
    write(
        request,
        {
            "case": str(case),
            "reference": str(reference) if reference else None,
            "replay": str(replay) if replay else None,
            "engine": args.engines[build],
            "timeout": args.timeout,
            "review": args.decisions.get(entry["id"], {}),
        },
    )
    result = directory / "result.json"
    try:
        installed = command(
            [calibre(args, "calibre-customize"), "-a", args.zips[build]],
            directory,
            "install",
            90,
            env,
        )
        if installed:
            outcome = {"status": "runner_error", "failures": ["plugin_install"]}
        else:
            status = command(
                [
                    calibre(args, "calibre-debug"),
                    "-e",
                    RUNTIME,
                    "--",
                    "lookup",
                    request,
                ],
                directory,
                "lookup",
                4 * args.timeout + 90,
                env,
            )
            if status == 0 and result.is_file():
                return directory, read(result)
            outcome = {"status": "runner_error", "failures": ["calibre_runtime"]}
    except subprocess.TimeoutExpired:
        outcome = {"status": "transient", "failures": ["process_timeout"]}
    write(result, outcome)
    return directory, outcome


def latest(run, case_id, build):
    """Locate the last completed attempt for a case and build."""
    results = sorted((run / case_id / build).glob("attempt-*/result.json"))
    return results[-1].parent if results else None


def run_case(args, state, run, entry):
    """Run the baseline and candidate, preserving all retry attempts."""
    case_id = entry["id"]
    baseline = latest(run, case_id, "baseline")
    for build in ("baseline", "candidate"):
        previous = latest(run, case_id, build)
        old = (
            latest(args.baseline_run, case_id, "baseline")
            if args.baseline_run
            else None
        )
        if build == "baseline" and old:
            baseline = old
            continue
        if build == "baseline" and args.replay_from:
            continue
        reference = baseline if build == "candidate" else None
        replay = latest(args.replay_from, case_id, build) if args.replay_from else None
        if args.replay_from and not replay:
            continue
        if previous:
            prior = read(previous / "result.json")
            if prior["status"] == "pass" or not args.retry_failed:
                continue
        for index in range(args.retries + 1):
            if index:
                time.sleep(15 * 2 ** (index - 1))
            previous, result = attempt(
                args, state, entry, build, run, reference, replay
            )
            if result["status"] != "transient" or replay:
                break
        if build == "baseline":
            baseline = previous


def report(state, run):
    """Write a detailed local report and a compact report safe for sharing."""
    manifest = read(state / "manifest.json")
    configuration = read(run / "builds.json")
    baseline_run = (
        Path(configuration["baseline_run"]) if configuration["baseline_run"] else run
    )
    rows = []
    for entry in manifest["cases"]:
        row = dict(entry)
        for build in ("baseline", "candidate"):
            directory = latest(
                baseline_run if build == "baseline" else run, entry["id"], build
            )
            row[build] = (
                read(directory / "result.json") if directory else {"status": "pending"}
            )
            row[build]["attempts"] = (
                len(list(directory.parent.glob("attempt-*/result.json")))
                if directory
                else 0
            )
        rows.append(row)
    write(run / "report.local.json", {"builds": configuration, "cases": rows})
    counts = {}
    baseline_counts = {}
    failures = {}
    for row in rows:
        result = row["candidate"]
        status = result["status"]
        baseline_status = row["baseline"]["status"]
        baseline_counts[baseline_status] = baseline_counts.get(baseline_status, 0) + 1
        counts[status] = counts.get(status, 0) + 1
        if status not in {"pass", "pending"}:
            for reason in result["failures"]:
                failures.setdefault(reason, []).append(row["id"])
    summary = {
        "books": manifest["counts"],
        "cases": len(rows),
        "selected_cases": len(configuration["cases"]),
        "mode": "replay" if configuration["replay_from"] else "live",
        "baseline_engine": configuration["baseline_engine"],
        "candidate_engine": configuration["candidate_engine"],
        "candidate": configuration["candidate_zip"],
        "baseline": configuration["baseline_zip"],
        "status": counts,
        "baseline_status": baseline_counts,
        "failure_groups": failures,
        "passed_after_retry": [
            row["id"]
            for row in rows
            if row["candidate"]["status"] == "pass" and row["candidate"]["attempts"] > 1
        ],
    }
    write(run / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    selected = set(configuration["cases"])
    return int(
        any(
            row["candidate"]["status"] != "pass"
            for row in rows
            if row["id"] in selected
        )
    )


def run_suite(args, state):
    """Pin build inputs and execute the local work queue only on explicit request."""
    manifest = read(state / "manifest.json")
    run = state / "runs" / args.name
    run.mkdir(parents=True, exist_ok=True)
    args.zips = {"baseline": args.baseline_zip, "candidate": args.candidate_zip}
    args.engines = {
        "baseline": args.baseline_engine,
        "candidate": args.candidate_engine,
    }
    if args.failed_from:
        source = state / "runs" / args.failed_from
        ids = {
            row["id"]
            for row in read(source / "report.local.json")["cases"]
            if row["candidate"]["status"] not in {"pass", "pending"}
        }
    else:
        ids = {row["id"] for row in manifest["cases"]}
    if not ids:
        raise SystemExit("No matching cases.")
    args.decisions = read(args.review) if args.review else {}
    args.baseline_run = (
        state / "runs" / args.reuse_baseline if args.reuse_baseline else None
    )
    args.replay_from = state / "runs" / args.replay if args.replay else None
    configuration = {
        **{f"{key}_zip": digest(path) for key, path in args.zips.items()},
        "baseline_engine": args.baseline_revision,
        "candidate_engine": args.candidate_revision,
        "engine_urls_sha256": hashlib.sha256(
            json.dumps(args.engines, sort_keys=True).encode()
        ).hexdigest(),
        "manifest": digest(state / "manifest.json"),
        "cases": sorted(ids),
        "review": digest(args.review) if args.review else None,
        "runner": digest(Path(__file__)),
        "runtime": digest(RUNTIME),
        "calibre": digest(calibre(args, "calibre-debug")),
        "baseline_run": str(args.baseline_run) if args.baseline_run else None,
        "replay_from": str(args.replay_from) if args.replay_from else None,
        "timeout": args.timeout,
    }
    if args.replay_from:
        source = read(args.replay_from / "builds.json")
        for key in ("manifest", "engine_urls_sha256"):
            if source[key] != configuration[key]:
                raise SystemExit(f"Replay input differs in {key}.")
        if any(not latest(args.replay_from, case_id, "candidate") for case_id in ids):
            raise SystemExit("Saved replay lacks one or more selected cases.")
    if args.baseline_run:
        old = read(args.baseline_run / "builds.json")
        for key in ("manifest", "baseline_zip", "baseline_engine", "calibre"):
            if old[key] != configuration[key]:
                raise SystemExit(f"Saved baseline differs in {key}.")
        if any(not latest(args.baseline_run, case_id, "baseline") for case_id in ids):
            raise SystemExit("Saved baseline lacks one or more selected cases.")
    pinned = run / "builds.json"
    if pinned.exists() and read(pinned) != configuration:
        raise SystemExit("Run inputs changed. Choose a new --name.")
    write(pinned, configuration)
    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        list(
            workers.map(
                lambda entry: run_case(args, state, run, entry),
                (entry for entry in manifest["cases"] if entry["id"] in ids),
            )
        )
    return report(state, run)


def label(value):
    """Validate a run name before using it as a path component."""
    if not value or not all(
        char.isascii() and (char.isalnum() or char in "-_") for char in value
    ):
        raise argparse.ArgumentTypeError(
            "Use letters, digits, hyphens, or underscores."
        )
    return value


def main():
    """Parse commands without reading a library or contacting providers by default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--calibre-bin", type=Path, default=RUNTIME.parent / "calibre")
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--database", required=True, type=Path)
    prepare.add_argument("--books", required=True, type=Path)
    prepare.add_argument(
        "--sample", type=int, default=20, choices=range(1, 101), metavar="1-100"
    )
    prepare.add_argument("--seed", default="pilot-1")
    run = commands.add_parser("run")
    run.add_argument("--name", required=True, type=label)
    for build in ("baseline", "candidate"):
        run.add_argument(f"--{build}-zip", required=True, type=Path)
        run.add_argument(f"--{build}-engine", required=True)
        run.add_argument(f"--{build}-revision", required=True)
    run.add_argument(
        "--timeout", type=int, default=30, choices=range(5, 181), metavar="5-180"
    )
    run.add_argument("--workers", type=int, default=2, choices=(1, 2))
    run.add_argument("--retries", type=int, default=2, choices=range(5), metavar="0-4")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--failed-from", type=label)
    run.add_argument("--reuse-baseline", type=label)
    run.add_argument("--replay", type=label)
    run.add_argument("--review", type=Path)
    commands.add_parser("report").add_argument("--name", required=True, type=label)
    args = parser.parse_args()
    os.umask(0o077)
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    if state.stat().st_mode & 0o077:
        parser.error("--state must be private (chmod 700)")
    args.calibre_bin = args.calibre_bin.resolve()
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "prepare":
            args.database = args.database.resolve()
            args.books = args.books.resolve()
            freeze(args, state)
            return 0
        if args.action == "report":
            return report(state, state / "runs" / args.name)
        args.baseline_zip = args.baseline_zip.resolve()
        args.candidate_zip = args.candidate_zip.resolve()
        return run_suite(args, state)


if __name__ == "__main__":
    raise SystemExit(main())
