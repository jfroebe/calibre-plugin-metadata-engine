"""Coordinate Calibre acceptance runs in the dedicated PostgreSQL database."""

import hashlib
import json
import os
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

DSN = (
    "host=192.168.0.59 port=5432 dbname=metadata_test user=metadata_test "
    "connect_timeout=5 application_name=calibre_library_acceptance "
    "options='-c statement_timeout=10000 -c lock_timeout=5000'"
)
DDL = (
    "CREATE SCHEMA IF NOT EXISTS calibre_acceptance",
    """
    CREATE TABLE IF NOT EXISTS calibre_acceptance.runs (
        id text PRIMARY KEY,
        name text NOT NULL,
        worker text NOT NULL,
        configuration jsonb NOT NULL,
        status text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        summary jsonb,
        diagnostics jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS calibre_acceptance.lookups (
        run_id text NOT NULL REFERENCES calibre_acceptance.runs(id),
        case_id text NOT NULL,
        build text NOT NULL CHECK (build IN ('baseline', 'candidate')),
        origin text NOT NULL,
        status text NOT NULL DEFAULT 'pending',
        attempt_count integer NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, case_id, build)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS calibre_acceptance.attempts (
        run_id text NOT NULL,
        case_id text NOT NULL,
        build text NOT NULL,
        attempt integer NOT NULL CHECK (attempt > 0),
        status text NOT NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz,
        result jsonb,
        PRIMARY KEY (run_id, case_id, build, attempt),
        FOREIGN KEY (run_id, case_id, build)
            REFERENCES calibre_acceptance.lookups(run_id, case_id, build)
    )
    """,
)


def connect():
    """Open a PostgreSQL session using the worker's libpq credentials."""
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Use the installed runner Python with psycopg.") from exc
    password_file = Path.home() / ".local/share/calibre-library-acceptance/pgpass"
    options = {}
    if password_file.is_file() and "PGPASSFILE" not in os.environ:
        options["passfile"] = str(password_file)
    try:
        return psycopg.connect(DSN, autocommit=True, **options)
    except psycopg.OperationalError as exc:
        # libpq failures can include server diagnostics. Print only the reason category.
        detail = str(exc).lower()
        if "password" in detail:
            reason = "authentication failed"
        elif "pg_hba" in detail:
            reason = "host access rejected"
        else:
            reason = "connection failed"
        raise SystemExit(f"PostgreSQL metadata_test: {reason}.") from None


def initialize(connection):
    """Create the dedicated tracking schema under a transactional setup lock."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (89411832,))
        for statement in DDL:
            connection.execute(statement)


def run_id(name, configuration):
    """Identify the same named sample and build pair from either worker."""
    identity = [
        name,
        configuration["manifest"],
        configuration["baseline_zip"],
        configuration["candidate_zip"],
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def compact(result):
    """Retain statuses and counters while leaving book metadata on the worker."""
    return {
        key: result[key]
        for key in (
            "status",
            "failures",
            "review",
            "cover",
            "grading_version",
            "plugin_version",
            "calibre_version",
            "provider_transient_errors",
            "mode",
        )
        if key in result
    }


class Tracker:
    """Record lookup transitions through the coordinator's PostgreSQL session."""

    def __init__(self, connection, identifier):
        """Serialize transactions from concurrent lookup threads."""
        self.connection = connection
        self.identifier = identifier
        self.mutex = Lock()

    def sync(self, case, build, directory, result=None):
        """Upsert an attempt and its current lookup state in one transaction."""
        number = int(directory.name.removeprefix("attempt-"))
        request = directory / "request.json"
        created = (
            request.stat().st_mtime if request.exists() else directory.stat().st_mtime
        )
        timestamp = datetime.fromtimestamp(created, UTC)
        status = result["status"] if result else "running"
        finished = (
            datetime.fromtimestamp((directory / "result.json").stat().st_mtime, UTC)
            if result
            else None
        )
        params = (self.identifier, case, build)
        with self.mutex, self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO calibre_acceptance.attempts
                    (run_id, case_id, build, attempt, status, started_at, finished_at, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, case_id, build, attempt) DO UPDATE SET
                    status = EXCLUDED.status, finished_at = EXCLUDED.finished_at,
                    result = EXCLUDED.result
                """,
                (
                    *params,
                    number,
                    status,
                    timestamp,
                    finished,
                    json.dumps(compact(result)) if result else None,
                ),
            )
            self.connection.execute(
                """
                UPDATE calibre_acceptance.lookups SET status=%s, attempt_count=%s
                WHERE run_id=%s AND case_id=%s AND build=%s AND attempt_count <= %s
                """,
                (status, number, *params, number),
            )
            self.connection.execute(
                "UPDATE calibre_acceptance.runs SET updated_at=clock_timestamp() WHERE id=%s",
                (self.identifier,),
            )

    def finish(self, status, summary=None, diagnostics=None):
        """Publish the run disposition and available aggregate reports."""
        self.connection.execute(
            """
            UPDATE calibre_acceptance.runs SET status=%s, updated_at=clock_timestamp(),
                summary=COALESCE(%s::jsonb, summary),
                diagnostics=COALESCE(%s::jsonb, diagnostics)
            WHERE id=%s
            """,
            (
                status,
                json.dumps(summary) if summary is not None else None,
                json.dumps(diagnostics) if diagnostics is not None else None,
                self.identifier,
            ),
        )


@contextmanager
def track(name, configuration, entries, builds):
    """Claim a shared run and register its lookup queue before doing any work."""
    identifier = run_id(name, configuration)
    worker = socket.gethostname()
    with connect() as connection:
        initialize(connection)
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (identifier,)
        ).fetchone()[0]
        if not acquired:
            raise SystemExit("Another worker is coordinating this named run.")
        private_paths = {"baseline_run", "replay_from"}
        public = {
            key: value
            for key, value in configuration.items()
            if key not in private_paths
        }
        public.update({key: bool(configuration.get(key)) for key in private_paths})
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO calibre_acceptance.runs (id, name, worker, configuration, status)
                VALUES (%s, %s, %s, %s::jsonb, 'running') ON CONFLICT (id) DO NOTHING
                """,
                (identifier, name, worker, json.dumps(public)),
            )
            owner, stored = connection.execute(
                "SELECT worker, configuration FROM calibre_acceptance.runs WHERE id=%s",
                (identifier,),
            ).fetchone()
            if owner != worker or stored != public:
                raise SystemExit(
                    "Run name is already pinned to different inputs or a different worker."
                )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO calibre_acceptance.lookups (run_id, case_id, build, origin)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """,
                    [
                        (identifier, entry["id"], build, entry["origin"])
                        for entry in entries
                        for build in builds
                    ],
                )
        tracker = Tracker(connection, identifier)
        tracker.finish("running")
        try:
            yield tracker
        except BaseException:
            if not connection.closed:
                try:
                    tracker.finish("interrupted")
                except Exception:
                    # Preserve the initiating error when the database is unreachable.
                    pass
            raise


def status():
    """List recent shared runs and completed or pending lookups by build."""
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT r.name, r.worker, r.status, l.build, l.status, count(*)
            FROM calibre_acceptance.runs r
            JOIN calibre_acceptance.lookups l ON l.run_id=r.id
            WHERE r.id IN (SELECT id FROM calibre_acceptance.runs
                           ORDER BY updated_at DESC LIMIT 20)
            GROUP BY r.id, r.name, r.worker, r.status, l.build, l.status
            ORDER BY r.updated_at DESC, l.build, l.status
            """
        )
        print(
            json.dumps(
                [
                    dict(
                        zip(
                            (
                                "run",
                                "worker",
                                "status",
                                "build",
                                "lookup_status",
                                "count",
                            ),
                            row,
                            strict=True,
                        )
                    )
                    for row in rows
                ],
                indent=2,
            )
        )


def preflight():
    """Verify authentication and initialize the tracking schema."""
    with connect() as connection:
        initialize(connection)
        print("PostgreSQL tracking schema is ready.")
