"""Deterministic engine proxy routing without probing the known-good relays."""

import asyncio
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_api import serve


@unittest.skipUnless(
    os.environ.get("CALIBRE_ENGINE_ROOT"),
    "Actual pinned engine checkout must be provided explicitly",
)
class ProxyRoutingTests(unittest.TestCase):
    """Exercise engine Runtime proxy inheritance and local routing rules."""

    def setUp(self):
        """Load the pinned engine and clear ambient credential-bearing proxies."""
        sys.path.insert(0, os.environ["CALIBRE_ENGINE_ROOT"])
        self.runtime_class = importlib.import_module(
            "metadata_engine.plugin_manager"
        ).Runtime
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.lower().endswith("_proxy")
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def request(self, network, url):
        """Use the actual engine HTTP client and always close its connections."""

        async def execute():
            runtime = self.runtime_class(
                "calibre_routing_test", {"http_timeout_seconds": 1}, network_cfg=network
            )
            try:
                return await runtime.request("GET", url)
            finally:
                await runtime.close()

        return asyncio.run(execute())

    def test_explicit_http_forwarding(self):
        """An otherwise unresolvable origin succeeds only through the relay."""
        with serve() as (relay, requests):
            response = self.request(
                {"proxy_url": relay, "trust_env": False},
                "http://calibre-fixture.invalid/health",
            )
            self.assertTrue(response.json()["ok"])
            self.assertEqual(requests[0][0], "/health")

    def test_environment_inheritance(self):
        """HTTP_PROXY is inherited when environment proxy support is enabled."""
        with serve() as (relay, _):
            with patch.dict(os.environ, {"http_proxy": relay}):
                response = self.request(
                    {"trust_env": True}, "http://calibre-fixture.invalid/health"
                )
                self.assertTrue(response.json()["ok"])

    def test_no_proxy_preserves_local_engine_routing(self):
        """Loopback engine traffic bypasses an intentionally broken relay."""
        with serve() as (origin, _):
            response = self.request(
                {
                    "proxy_url": "http://127.0.0.1:9",
                    "no_proxy": ["127.0.0.1"],
                    "trust_env": False,
                },
                origin + "/health",
            )
            self.assertTrue(response.json()["ok"])

    def test_process_proxy_settings_do_not_mutate_parent(self):
        """Per-process environment changes remain confined to that child."""
        original = dict(os.environ)
        child = dict(original, http_proxy="http://127.0.0.1:9", no_proxy="localhost")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                'import os; assert os.environ["http_proxy"] == '
                '"http://127.0.0.1:9"; assert os.environ["no_proxy"] == "localhost"',
            ],
            env=child,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(dict(os.environ), original)

    def test_connection_failure_is_bounded(self):
        """A broken proxy raises without requiring external infrastructure."""
        import httpx

        with self.assertRaises(httpx.TransportError):
            self.request(
                {"proxy_url": "http://127.0.0.1:9", "trust_env": False},
                "http://calibre-fixture.invalid/health",
            )
