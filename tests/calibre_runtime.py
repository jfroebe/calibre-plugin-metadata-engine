"""Run with calibre-debug: exercise the installed ZIP with real Calibre APIs."""

from __future__ import annotations

import importlib
import json
import logging
import os
import struct
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from queue import Queue
from threading import Event
from urllib.request import ProxyHandler, build_opener, install_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibre.customize.ui import metadata_plugins
from calibre.ebooks.metadata.sources.base import Source
from fixture_api import ROWS, png, serve


class InstalledPluginTests(unittest.TestCase):
    """No Calibre stubs: namespace, conversion and fallback run in Calibre."""

    def setUp(self):
        """Locate the registered plugin and configure an isolated fixture API."""
        self.plugin = next(
            p
            for p in metadata_plugins(("identify", "cover"))
            if p.name == "Metadata Engine"
        )
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
                self.assertIn(
                    "metadata-engine:fixture", [tag.casefold() for tag in actual.tags]
                )

    def test_cached_hardcover_provider_tag_is_not_a_book_tag(self):
        """Filter the old source marker while preserving actual subjects."""
        row = {
            "title": "Dark and Stabby",
            "authors": ["Alisha Williams"],
            "source": "hardcover",
            "tags": ["Hardcover", "Anthologies"],
            "genres": ["Romance"],
        }
        self.plugin.prefs["include_source_tag"] = False
        metadata = self.plugin._to_metadata(row, 0)
        self.assertEqual(metadata.tags, ["Romance", "Anthologies"])
        self.plugin.prefs["include_source_tag"] = True
        metadata = self.plugin._to_metadata(row, 0)
        self.assertIn("metadata-engine:hardcover", metadata.tags)
        self.plugin.prefs["include_source_tag"] = False
        apple = {
            "title": "City of Demons",
            "authors": ["Richelle Mead"],
            "source": "itunes",
            "tags": ["itunes", "books"],
            "genres": ["Paranormal Romance"],
        }
        self.assertEqual(
            self.plugin._to_metadata(apple, 0).tags,
            ["Paranormal Romance", "books"],
        )

    def test_ebook_enriches_matching_book_not_different_author(self):
        """Merge rich ebook details only into the same title and author."""
        rows = [
            {
                "title": "City of Demons",
                "authors": ["Richelle Mead"],
                "source": "goodreads",
                "source_id": "1",
                "confidence": 0.99,
            },
            {
                "title": "City of Demons",
                "authors": ["Richelle Mead"],
                "source": "itunes",
                "source_id": "2",
                "confidence": 0.98,
                "description": "Real synopsis",
                "cover_url": "https://example.test/a.jpg",
                "genres": ["Paranormal Romance"],
            },
            {
                "title": "City of Demons",
                "authors": ["Another Author"],
                "source": "fixture",
                "source_id": "3",
                "confidence": 0.97,
            },
        ]
        merged = self.plugin._dedupe(rows)
        selected = next(row for row in merged if row.get("source") == "goodreads")
        other = next(row for row in merged if row.get("source") == "fixture")
        self.assertEqual(selected["description"], "Real synopsis")
        self.assertEqual(selected["genres"], ["Paranormal Romance"])
        self.assertNotIn("description", other)

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

    def test_duplicate_edition_preserves_description_and_cover(self):
        """A higher-ranked sparse record cannot erase another provider's content."""
        for providers in ("sparse,rich", "rich,sparse"):
            with self.subTest(providers=providers):
                self.plugin.prefs["plugin_ids"] = providers
                results = self.identify(title="The Hobbit")
                self.assertEqual(len(results), 1)
                self.assertEqual(
                    results[0].comments, "The matching edition's full description."
                )
                self.assertTrue(results[0].has_cached_cover)

    def test_duplicate_edition_cover_survives_sparse_or_broken_winner(self):
        """Keep every same-edition cover URL until an actual download succeeds."""
        for providers in ("sparse,rich", "bad-cover,rich", "rich,bad-cover"):
            with self.subTest(providers=providers):
                self.plugin._cover_urls.clear()
                self.plugin.prefs["plugin_ids"] = providers
                queue = Queue()
                self.plugin.download_cover(
                    self.log,
                    queue,
                    self.abort,
                    title="The Hobbit",
                    identifiers={"isbn": ROWS[0]["isbn"]},
                    timeout=2,
                )
                self.assertEqual(queue.qsize(), 1)
                self.assertEqual(queue.get()[1], png(600, 900))

    def test_description_fill_does_not_cross_edition_or_overwrite_existing(self):
        """Fill only identical keys and leave caller-owned provider rows intact."""
        sparse = dict(ROWS[0], description=" ", confidence=1.0)
        rich = dict(sparse, description="Matching description", confidence=0.8)
        wrong = dict(rich, isbn="9780441172719", description="Wrong edition")
        merged = self.plugin._dedupe([sparse, wrong, rich])
        match = next(row for row in merged if row["isbn"] == sparse["isbn"])
        self.assertEqual(match["description"], "Matching description")
        self.assertEqual(sparse["description"], " ")
        populated = dict(sparse, description="Preferred description")
        self.assertEqual(
            self.plugin._dedupe([rich, populated])[0]["description"],
            "Preferred description",
        )

    def test_title_query_and_cross_provider_content_for_curvy(self):
        """Separate title/author and retain content from an identifier-less match."""
        self.assertEqual(
            self.plugin._make_query(
                "Curvy", ["Alexa Riley"], {"isbn": "9781978363632"}
            ),
            "Curvy",
        )
        book = dict(title="Curvy", authors=["Alexa Riley"], confidence=0.98)
        google = dict(
            book,
            source="google_books",
            isbn="9781978363632",
            description="Provider description",
            cover_url=None,
        )
        hardcover = dict(
            book,
            source="hardcover",
            source_id="curvy",
            isbn=None,
            description=None,
            cover_url=self.url + "/full.png",
        )
        results = self.plugin._dedupe([hardcover, google])
        for row in results:
            self.assertEqual(row["description"], "Provider description")
            self.assertEqual(row["cover_url"], self.url + "/full.png")
        self.assertIsNone(hardcover["description"])

    def test_calibre_merge_keeps_isbnless_matching_book(self):
        """Calibre's normal identify merger must retain the best title match."""
        from calibre.ebooks.metadata.sources.identify import identify
        from calibre.utils.logging import DevNull

        self.plugin.prefs["plugin_ids"] = "curvy-merge"
        results = identify(
            DevNull(),
            self.abort,
            title="Curvy",
            authors=["Alexa Riley"],
            identifiers={"mobi-asin": "B01CDZAQTY"},
            timeout=5,
            allowed_plugins=["Metadata Engine"],
        )
        matches = [
            item
            for item in results
            if item.title == "Curvy" and item.authors == ["Alexa Riley"]
        ]
        self.assertEqual(len(matches), 1)
        self.assertIn("matching book description", matches[0].comments)
        self.assertTrue(any(item.title == "My Curvy Belle" for item in results))

    def test_mobi_asin_sent_for_identify_and_cover_matching(self):
        """Forward Calibre's Kindle identifier and prefer its matching cover."""
        self.plugin.prefs["plugin_ids"] = "fixture"
        self.identify(
            title="A Specter and an Invasion (Legacy of the Lost Book 3)",
            authors=["M. D. Cooper", "Chris J. Pike", "Jill Cooper"],
            identifiers={"mobi-asin": "B08CK7H7P2"},
        )
        searches = [
            query for path, query in self.requests if path == "/plugins/fixture/search"
        ]
        self.assertTrue(searches)
        self.assertEqual(searches[0].get("asin"), ["B08CK7H7P2"])
        self.assertEqual(
            self.plugin._cover_identifier_score(
                {"asin": "B08CK7H7P2"}, {"mobi-asin": "B08CK7H7P2"}
            ),
            2,
        )
        self.assertEqual(
            self.plugin._make_query(None, None, {"mobi-asin": "B08CK7H7P2"}),
            "B08CK7H7P2",
        )

    def test_calibre_merge_with_mobi_asin_and_multiple_authors(self):
        """Retain an identifier-dependent result through Calibre's real merger."""
        from calibre.ebooks.metadata.sources.identify import identify
        from calibre.utils.logging import DevNull

        self.plugin.prefs["plugin_ids"] = "specter"
        results = identify(
            DevNull(),
            self.abort,
            title="A Specter and an Invasion (Legacy of the Lost Book 3)",
            authors=["M. D. Cooper", "Chris J. Pike", "Jill Cooper"],
            identifiers={"mobi-asin": "B08CK7H7P2"},
            timeout=5,
            allowed_plugins=["Metadata Engine"],
        )
        self.assertTrue(results)
        self.assertEqual(
            results[0].title,
            "A Specter and an Invasion",
        )
        self.assertEqual(results[0].series, "Legacy of the Lost")
        self.assertEqual(results[0].series_index, 3.0)
        self.assertEqual(results[0].get_identifiers().get("asin"), "B08CK7H7P2")
        self.assertEqual(
            results[0].authors, ["M. D. Cooper", "Chris J. Pike", "Jill Cooper"]
        )
        self.assertFalse(results[0].comments)

    def test_series_title_fallback_is_conservative(self):
        """Extract explicit numbered series while preserving structured values."""
        def convert(title, series=None):
            return self.plugin._to_metadata(
                {"title": title, "authors": ["Example"], "series": series}, 0
            )

        inferred = convert("A Title (Some Series Book 2)")
        self.assertEqual((inferred.title, inferred.series, inferred.series_index),
                         ("A Title", "Some Series", 2.0))
        structured = convert("A Title (Other Book 2)",
                             [{"name": "Official", "index": 5}])
        self.assertEqual((structured.title, structured.series, structured.series_index),
                         ("A Title", "Official", 5.0))
        unnumbered = convert("A Title (A Novel)")
        self.assertEqual(unnumbered.title, "A Title (A Novel)")
        self.assertFalse(unnumbered.series)

    def test_mixed_media_excludes_movies_from_metadata_and_covers(self):
        """A movie sharing the book ISBN must not replace its metadata or art."""
        self.plugin.prefs["plugin_ids"] = "mixed"
        results = self.identify(
            title="The Hobbit", identifiers={"isbn": "9780547928227"}
        )
        self.assertEqual([row.title for row in results], ["The Hobbit"])
        queue = Queue()
        self.plugin.download_cover(
            self.log, queue, self.abort, identifiers={"isbn": "9780547928227"}
        )
        self.assertEqual(struct.unpack("!II", queue.get_nowait()[1][16:24]), (600, 900))
        self.assertNotIn("/invalid.png", [path for path, _ in self.requests])

    def test_structural_schema_failures_are_actionable_and_provider_isolated(self):
        """Valid JSON with a wrong shape must fail explicitly, not look empty."""
        for provider in ("array", "missing", "mapping", "row", "title"):
            with self.subTest(schema=provider):
                with self.assertRaisesRegex(ValueError, "API schema"):
                    self.plugin._search("schema-" + provider, "The Hobbit", None, {}, 2)
                self.plugin.prefs["plugin_ids"] = "schema-" + provider + ",fixture"
                with self.assertLogs(self.log, level="ERROR") as captured:
                    self.assertEqual(len(self.identify(title="The Hobbit")), 1)
                self.assertIn("API schema", " ".join(captured.output))

    def test_discovery_schema_failure_is_actionable(self):
        """Discovery with a non-array inventory reports the incompatible field."""
        self.plugin.prefs["base_url"] = self.url + "/bad-discovery"
        with self.assertRaisesRegex(ValueError, "plugins must be an array"):
            self.plugin._discover(2, self.abort)

    def test_download_selects_exact_edition_full_resolution(self):
        """Download the exact edition's full cover, not a higher-ranked thumbnail."""
        self.plugin.prefs["plugin_ids"] = "editions"
        queue = Queue()
        self.plugin.download_cover(
            self.log,
            queue,
            self.abort,
            title="The Hobbit",
            identifiers={"isbn": "9780547928227"},
        )
        self.assertEqual(queue.get_nowait()[1], png(600, 900))
        self.assertIn("/full.png", [path for path, _ in self.requests])
        self.assertNotIn("/thumbnail.png", [path for path, _ in self.requests])

    def test_known_wrong_edition_cover_is_not_used(self):
        """A supplied ISBN mismatch cannot become a fallback cover."""
        self.plugin.prefs["plugin_ids"] = "wrong-edition"
        queue = Queue()
        self.plugin.download_cover(
            self.log,
            queue,
            self.abort,
            title="The Hobbit",
            identifiers={"isbn": "9780547928227"},
        )
        self.assertTrue(queue.empty())
        self.assertNotIn("/thumbnail.png", [path for path, _ in self.requests])

    def test_client_proxy_inheritance_and_loopback_bypass(self):
        """Fresh Calibre clients inherit HTTP forwarding and respect NO_PROXY."""
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        select = (
            "from calibre.customize.ui import metadata_plugins; "
            "p=next(p for p in metadata_plugins(('identify','cover')) "
            "if p.name=='Metadata Engine'); "
        )
        for mode, proxy, bypass, url in (
            ("forward", self.url, "", "http://calibre-fixture.invalid/health"),
            ("bypass", "http://127.0.0.1:9", "127.0.0.1", self.url + "/health"),
        ):
            with self.subTest(mode=mode):
                child = dict(
                    env,
                    http_proxy=proxy,
                    HTTP_PROXY=proxy,
                    no_proxy=bypass,
                    NO_PROXY=bypass,
                )
                result = subprocess.run(
                    [
                        os.environ["CALIBRE_TEST_DEBUG"],
                        "-c",
                        select
                        + f"import json; print(json.dumps(p._http_json({url!r}, 2)))",
                    ],
                    env=child,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # Calibre can print its proxy banner before the JSON response.
                self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])
        self.assertEqual(sum(path == "/health" for path, _ in self.requests), 2)

    def test_proxy_outage_retains_cached_cover_and_recovery_uses_it(self):
        """A real failed proxy connection must leave the cover cache intact."""
        cached_url = "http://calibre-cover.invalid/cached.png"
        self.plugin._cover_urls[("isbn", "9780547928227")] = cached_url
        self.plugin.prefs["plugin_ids"] = "missing"
        original = urllib.request._opener
        self.addCleanup(install_opener, original)
        install_opener(build_opener(ProxyHandler({"http": "http://127.0.0.1:9"})))
        queue = Queue()
        self.plugin.download_cover(
            self.log,
            queue,
            self.abort,
            identifiers={"isbn": "9780547928227"},
            timeout=1,
        )
        self.assertTrue(queue.empty())
        self.assertEqual(
            self.plugin._cached_cover({"isbn": "9780547928227"}), cached_url
        )
        install_opener(build_opener(ProxyHandler({"http": self.url})))
        self.plugin.download_cover(
            self.log, queue, self.abort, identifiers={"isbn": "9780547928227"}
        )
        self.assertEqual(queue.get_nowait()[1], png())
        self.assertEqual(
            self.plugin._cached_cover({"isbn": "9780547928227"}), cached_url
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstalledPluginTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun != 26:
        raise RuntimeError("The real-Calibre regression suite was not fully collected")
    sys.exit(0 if result.wasSuccessful() else 1)
