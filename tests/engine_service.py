"""Run the pinned engine with a test-only provider and disposable SQLite DB."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import hashlib
from pathlib import Path
import sys

import uvicorn
from fastapi import Response

from fixture_api import png


async def run(engine_root: Path, work: Path, port: int, pg_dsn: str = "") -> None:
    """Load the actual engine entry point without replacing API handlers."""
    sys.path.insert(0, str(engine_root))
    spec = importlib.util.spec_from_file_location(
        "engine_entry", engine_root / "metadata_engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider_dir = work / "providers"
    provider_dir.mkdir(exist_ok=True)
    provider_dir.joinpath("fixture.py").write_text(
        Path(__file__).with_name("engine_provider.py").read_text("utf-8"),
        encoding="utf-8",
    )
    provider_dir.joinpath("fixture.manifest.json").write_text(
        json.dumps(
            {
                "id": "calibre_fixture",
                "name": "Calibre fixture",
                "author": "Test suite",
                "license": "GPL-3.0-only",
                "version": "1.0.0",
                "type": "plugin",
                "metadata_types": ["book"],
                "access_target": "test fixtures",
                "entry_point": "fixture.py:FixtureProvider",
                "default_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    plugin_config = work / "providers.json"
    plugin_config.write_text(
        json.dumps(
            {
                "ME_PLUGIN_CALIBREFIXTURE": {
                    "ENABLED": True,
                    "COVER_BASE": f"http://127.0.0.1:{port}",
                    "CACHE_REFRESH_AFTER_SECONDS": 3600,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = {
        "server": {"host": "127.0.0.1", "port": port},
        "plugins": {
            "directories": [str(provider_dir)],
            "config_file": str(plugin_config),
        },
        "adapters": {"directories": []},
        "database": {
            "backend": "sqlite",
            "sqlite": {"path": str(work / "cache.sqlite3")},
        },
        "evidence": {"enabled": True},
    }
    if pg_dsn:
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict

        connection = conninfo_to_dict(pg_dsn)
        if (
            connection.get("host") != "127.0.0.1"
            or connection.get("dbname") != "postgres"
        ):
            raise ValueError(
                "Only a disposable loopback PostgreSQL test service is accepted"
            )
        database = "calibre_" + hashlib.sha256(str(work).encode()).hexdigest()[:16]
        with psycopg.connect(pg_dsn, autocommit=True) as admin:
            if not admin.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone():
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                )
        cfg["database"] = {
            "backend": "postgresql",
            "postgresql": {
                "host": "127.0.0.1",
                "port": int(connection["port"]),
                "database": database,
                "user": connection.get("user", "postgres"),
                "password": connection.get("password", ""),
                "pool_min": 1,
                "pool_max": 4,
            },
        }
    app = await module.build(cfg, work)

    @app.post("/test-provider-down")
    async def down():
        app.state.plugins.plugins["calibre_fixture"].failure = True
        return {"ok": True}

    @app.get("/test-cover.png")
    async def cover():
        return Response(png(), media_type="image/png")

    @app.get("/test-provider-calls")
    async def calls():
        return {"calls": app.state.plugins.plugins["calibre_fixture"].calls}

    await uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
    ).serve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--pg-dsn", default="")
    args = parser.parse_args()
    asyncio.run(
        run(args.engine_root.resolve(), args.work.resolve(), args.port, args.pg_dsn)
    )
