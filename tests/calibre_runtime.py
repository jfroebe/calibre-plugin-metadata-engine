"""Run with calibre-debug: exercise the installed ZIP with real Calibre APIs."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from queue import Queue
import sys
from threading import Event
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture_api import ROWS, png, serve

from calibre.customize.ui import metadata_plugins
from calibre.ebooks.metadata.sources.base import Source


class InstalledPluginTests(unittest.TestCase):
    """No Calibre stubs: namespace, conversion and fallback run in Calibre."""

    def setUp(self):
        """Locate the registered plugin and configure an isolated fixture API."""
        self.plugin = next(p for p in metadata_plugins() if p.name == "Metadata Engine")
        self.server = serve()
        self.url, self.requests = self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.plugin.prefs["base_url"] = self.url
        self.plugin.prefs["plugin_ids"] = ""
        self.plugin.prefs["include_source_tag"] = True
        self.plugin.prefs["api_token"] = ""
        self.plugin._cover_urls.clear()
        self.log = logging.getLogger("calibre-validation")
        self.abort = Event()

    def identify(self, **kwargs):
        """Invoke Calibre's metadata source interface and drain its results."""
        queue = Queue()
        error = self.plugin.identify(self.log, queue, self.abort, **kwargs)
        self.assertIsNone(error)
        return list(queue.queue)

    def test_namespace_configuration_and_source_registration(self):
        """Resolve both packaged modules through Calibre's import namespace."""
        module = importlib.import_module("calibre_plugins.metadata_engine")
        config = importlib.import_module("calibre_plugins.metadata_engine.config")
        self.assertIsInstance(self.plugin, Source)
        self.assertIs(module.MetadataEngineSource, type(self.plugin))
        self.assertTrue(hasattr(config, "ConfigWidget"))
        self.assertEqual(self.plugin.capabilities, frozenset(("identify", "cover")))
        defaults = {option.name: option.default for option in self.plugin.options}
        self.assertEqual(defaults["base_url"], "http://127.0.0.1:8790")
        self.assertEqual(defaults["plugin_ids"], "")
        self.assertEqual(defaults["max_results"], 20)
        self.assertTrue(defaults["verify_ssl"])

    def test_required_book_fixtures(self):
        """Check values, identifiers and provenance for every fixture edition."""
        for expected in ROWS:
            with self.subTest(title=expected["title"], isbn=expected.get("isbn")):
                identifiers = {
                    key: expected[key] for key in ("isbn", "asin") if expected.get(key)
                }
                results = self.identify(
                    title=expected["title"], identifiers=identifiers
                )
                self.assertEqual(len(results), 1)
                actual = results[0]
                self.assertEqual(actual.title, expected["title"])
                for key, value in identifiers.items():
                    self.assertEqual(actual.get_identifiers()[key], value)
                self.assertEqual(actual.authors, expected.get("authors", ["Unknown"]))
                if expected.get("publisher"):
                    self.assertEqual(actual.publisher, expected["publisher"])
                if expected.get("published_year"):
                    self.assertEqual(
                        actual.pubdate.year, int(expected["published_year"])
                    )
                if expected.get("description"):
                    self.assertEqual(actual.comments, expected["description"])
                self.assertIn("metadata-engine:fixture", actual.tags)

    def test_unknown_title_has_no_invented_matches(self):
        """An unknown title must leave the queue empty."""
        self.assertEqual(self.identify(title="Fictional Unfindable 72941"), [])

    def test_discovery_skips_disabled_and_isolates_provider_outage(self):
        """One 502 must not suppress another provider's metadata."""
        self.assertEqual(self.identify(title="The Hobbit")[0].title, "The Hobbit")
        paths = [path for path, _ in self.requests]
        self.assertIn("/plugins/broken/search", paths)
        self.assertNotIn("/plugins/disabled/search", paths)

    def test_explicit_provider_avoids_discovery(self):
        """Selected provider requests must bypass automatic discovery."""
        self.plugin.prefs["plugin_ids"] = "fixture"
        self.identify(title="Dune", identifiers={"isbn": "9780441013593"})
        self.assertNotIn("/plugins", [path for path, _ in self.requests])
        self.assertEqual(self.requests[-1][1]["isbn"], ["9780441013593"])

    def test_failures_are_isolated(self):
        """404, 401 and invalid JSON must permit the next provider to succeed."""
        for failing in ("missing", "unauthorized", "malformed"):
            with self.subTest(provider=failing):
                self.plugin.prefs["plugin_ids"] = failing + ",fixture"
                self.assertEqual(len(self.identify(title="The Hobbit")), 1)

    def test_cover_outage_invalid_image_and_cache_preservation(self):
        """A stale invalid candidate falls through and valid cover is cached."""
        self.plugin._cover_urls[("isbn", "9780547928227")] = self.url + "/invalid.png"
        queue = Queue()
        self.plugin.download_cover(
            self.log,
            queue,
            self.abort,
            title="The Hobbit",
            identifiers={"isbn": "9780547928227"},
        )
        self.assertEqual(queue.get_nowait()[1], png())
        self.assertEqual(
            self.plugin._cached_cover({"isbn": "9780547928227"}),
            self.url + "/cover.png",
        )
        self.plugin.prefs["plugin_ids"] = "missing"
        again = Queue()
        self.plugin.download_cover(
            self.log, again, self.abort, identifiers={"isbn": "9780547928227"}
        )
        self.assertEqual(again.get_nowait()[1], png())

    def test_edition_cover_ranking(self):
        """Exact ISBN must outrank unrelated high-confidence artwork."""
        exact = self.plugin._cover_identifier_score(
            {"isbn": "978-0-547-92822-7"}, {"isbn": "9780547928227"}
        )
        wrong = self.plugin._cover_identifier_score(
            {"isbn": "9780441172719"}, {"isbn": "9780547928227"}
        )
        self.assertGreater(exact, wrong)

    def test_timeout_and_abort(self):
        """A timeout is bounded and an already aborted lookup does no I/O."""
        self.plugin.prefs["plugin_ids"] = "slow,fixture"
        self.assertEqual(len(self.identify(title="The Hobbit", timeout=1)), 1)
        before = len(self.requests)
        self.abort.set()
        self.assertEqual(self.identify(title="The Hobbit"), [])
        self.assertEqual(len(self.requests), before)

    def test_conflicting_sources_deduplicate_and_rank(self):
        """Duplicate ISBNs retain the higher-confidence provenance."""
        rows = self.plugin._dedupe(
            [
                {
                    "title": "Dune",
                    "isbn": "9780441172719",
                    "confidence": 0.3,
                    "source": "low",
                },
                {
                    "title": "Dune",
                    "isbn": "9780441172719",
                    "confidence": 0.9,
                    "source": "high",
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "high")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstalledPluginTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun != 10:
        raise RuntimeError("The real-Calibre regression suite was not fully collected")
    sys.exit(0 if result.wasSuccessful() else 1)
