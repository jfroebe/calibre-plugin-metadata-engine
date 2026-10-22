"""Opt-in local-worker checks for the established HTTP and SOCKS5 relays."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote


async def main() -> None:
    """Exercise the pinned engine Runtime and emit no credential values."""
    if not os.environ.get("CALIBRE_SOCKS_PASSWORD"):
        raise SystemExit("Missing CALIBRE_SOCKS_PASSWORD secret")
    sys.path.insert(0, os.environ["CALIBRE_ENGINE_ROOT"])
    from metadata_engine.plugin_manager import Runtime

    results = []
    password = quote(os.environ["CALIBRE_SOCKS_PASSWORD"], safe="")
    modes = {
        "http-forward": ("http://192.168.0.59:8118", "http://example.com"),
        "https-connect": ("http://192.168.0.59:8118", "https://example.com"),
        "socks5-auth": (
            f"socks5://socks5:{password}@192.168.0.59:9118",
            "https://example.com",
        ),
        "socks5h-remote-dns": (
            f"socks5h://socks5:{password}@192.168.0.59:9118",
            "https://example.com",
        ),
        "socks5-auth-rejected": (
            "socks5h://socks5:intentionally-invalid@192.168.0.59:9118",
            "https://example.com",
        ),
        "connection-failure": ("http://127.0.0.1:9", "https://example.com"),
    }
    for name, (proxy, target) in modes.items():
        runtime = Runtime(
            "calibre_proxy_test",
            {
                "http_timeout_seconds": 10,
            },
            network_cfg={"proxy_url": proxy, "trust_env": False},
        )
        expected_failure = name in {"socks5-auth-rejected", "connection-failure"}
        failed = False
        try:
            response = await runtime.request("GET", target)
            response.raise_for_status()
            passed = not expected_failure
        except Exception:
            # Runtime may include endpoint details in exceptions. Never persist
            # those messages or traces for authenticated-proxy tests.
            failed = True
            passed = expected_failure
        finally:
            await runtime.close()
        results.append({"test": name, "passed": passed, "transport_failed": failed})
    report = Path(os.environ.get("CALIBRE_PROXY_REPORT", "proxy-report.json"))
    report.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if not all(row["passed"] for row in results):
        raise SystemExit("Proxy qualification failed; see sanitized report")


if __name__ == "__main__":
    asyncio.run(main())
