"""Actual pinned-engine route and persistent cache integration regressions."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime", ROOT / "ci/run_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@unittest.skipUnless(
    os.environ.get("CALIBRE_ENGINE_ROOT"),
    "Actual engine checkout must be provided explicitly",
)
class ActualEngineTests(unittest.TestCase):
    """Use actual engine routes, serialization, provider loader and SQLite."""

    def test_api_contract_and_cache(self):
        """Two HTTP lookups must return expected fields and call provider once."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports, work = root / "reports", root / "engine"
            reports.mkdir()
            work.mkdir()
            with runtime.engine(
                Path(os.environ["CALIBRE_ENGINE_ROOT"]), work, reports
            ) as (url, opener):

                def get(path):
                    with opener.open(url + path, timeout=5) as response:
                        return json.load(response)

                self.assertTrue(get("/health")["ok"])
                inventory = get("/plugins")["plugins"]
                self.assertEqual(inventory[0]["plugin_id"], "calibre_fixture")
                route = (
                    "/plugins/calibre_fixture/search?query=The%20Hobbit"
                    "&isbn=9780547928227"
                )
                first, second = get(route), get(route)
                self.assertEqual(first, second)
                row = first["results"][0]
                self.assertEqual(row["title"], "The Hobbit")
                self.assertEqual(row["isbn"], "9780547928227")
                self.assertEqual(row["authors"], ["J. R. R. Tolkien"])
                self.assertEqual(row["metadata_type"], "book")
                self.assertEqual(get("/test-provider-calls")["calls"], 1)
                self.assertEqual(
                    get("/plugins/calibre_fixture/search?query=Unfindable72941")[
                        "results"
                    ],
                    [],
                )
                with self.assertRaises(HTTPError) as caught:
                    get("/plugins/missing/search?query=x")
                self.assertEqual(caught.exception.code, 404)
