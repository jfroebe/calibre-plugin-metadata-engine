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
from unittest.mock import patch
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

    def test_specter_without_identifier_combines_partial_sources(self):
        """A title and authors lookup must return one complete leading match."""
        authors = ["M. D. Cooper", "Chris J. Pike", "Jill Cooper"]
        amazon = {
            "title": "A Specter and an Invasion",
            "authors": [],
            "series": [{"name": "Legacy of the Lost", "position": 3}],
            "asin": "B08CK7H7P2",
            "source": "amazon",
            "confidence": 0.99,
            "metadata_type": "book",
        }
        goodreads = {
            "title": "A Specter and an Invasion (Legacy of the Lost #3)",
            "authors": ["Chris J. Pike", "M. D. Cooper", "Jill Cooper"],
            "source": "goodreads",
            "source_id": "55033599",
            "description": "President Finley threatens the remaining free nations.",
            "confidence": 0.90,
            "metadata_type": "book",
        }

        def search(provider, *args, **kwargs):
            return [dict(amazon if provider == "amazon" else goodreads)]

        with (
            patch.object(
                self.plugin,
                "_configured_provider_ids",
                return_value=["amazon", "goodreads"],
            ),
            patch.object(self.plugin, "_search", side_effect=search),
        ):
            results = self.identify(
                title="A Specter and an Invasion (Legacy of the Lost Book 3)",
                authors=authors,
                identifiers={},
            )
        self.assertTrue(results)
        match = results[0]
        self.assertEqual(match.title, "A Specter and an Invasion")
        self.assertEqual(match.authors, authors)
        self.assertEqual(match.series, "Legacy of the Lost")
        self.assertEqual(match.series_index, 3)
        self.assertIn("President Finley", match.comments)

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
                self.assertNotIn(
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
        self.assertEqual(metadata.tags, ["Romance", "Anthologies"])
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

    def test_store_and_source_labels_are_not_subjects_or_description(self):
        """Keep subjects and synopsis while discarding source and storefront markers."""
        row = {
            "title": "A Specter and an Invasion",
            "source": "goodreads",
            "genres": [
                "Kindle Store",
                "Kindle eBooks",
                "Science Fiction & Fantasy",
                "Science Fiction",
                "Space Opera",
            ],
            "tags": ["goodreads", "audiobooks", "metadata-engine:goodreads"],
            "description": "Source: Goodreads\\nTime has passed. Goodreads is mentioned in prose.",
        }
        metadata = self.plugin._to_metadata(row, 0)
        self.assertEqual(
            metadata.tags,
            ["Science Fiction & Fantasy", "Science Fiction", "Space Opera"],
        )
        self.assertEqual(
            metadata.comments, "Time has passed. Goodreads is mentioned in prose."
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
        searches = [
            query for path, query in self.requests if path == "/plugins/fixture/search"
        ]
        self.assertEqual(searches[0]["isbn"], ["9780441013593"])

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

        self.plugin.prefs["plugin_ids"] = "specter,goodreads"
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
        self.assertIn("remaining free nations", results[0].comments)
        searches = [
            query
            for path, query in self.requests
            if path == "/plugins/goodreads/search"
        ]
        self.assertEqual(len(searches), 2)
        self.assertEqual(searches[-1]["query"], ["A Specter and an Invasion"])
        self.assertNotIn("author", searches[-1])

    def test_series_title_fallback_is_conservative(self):
        """Extract explicit numbered series while preserving structured values."""

        def convert(title, series=None):
            return self.plugin._to_metadata(
                {"title": title, "authors": ["Example"], "series": series}, 0
            )

        inferred = convert("A Title (Some Series Book 2)")
        self.assertEqual(
            (inferred.title, inferred.series, inferred.series_index),
            ("A Title", "Some Series", 2.0),
        )
        structured = convert(
            "A Title (Other Book 2)", [{"name": "Official", "index": 5}]
        )
        self.assertEqual(
            (structured.title, structured.series, structured.series_index),
            ("A Title", "Official", 5.0),
        )
        unnumbered = convert("A Title (A Novel)")
        self.assertEqual(unnumbered.title, "A Title (A Novel)")
        self.assertFalse(unnumbered.series)

    def test_description_fallback_requires_same_work_and_authors(self):
        """Do not copy a synopsis across different works, authors, or editions."""
        book = {
            "title": "A Specter and an Invasion (Aeon 14: Legacy of the Lost Book 3)",
            "authors": ["M. D. Cooper", "Chris J. Pike", "Jill Cooper"],
            "asin": "B08CK7H7P2",
            "confidence": 0.9,
        }
        donor = {
            "title": "A Specter and an Invasion (Legacy of the Lost #3)",
            "authors": ["Chris J. Pike", "M.D. Cooper", "Jill Cooper"],
            "description": "matching synopsis",
        }

        def result(other):
            return next(
                row for row in self.plugin._dedupe([book, other]) if row.get("asin")
            )

        self.assertEqual(result(donor).get("description"), "matching synopsis")
        self.assertFalse(
            result(dict(donor, authors=["Another Writer"])).get("description")
        )
        self.assertFalse(
            result(
                dict(donor, title="A Specter and an Invasion (Legacy of the Lost #2)")
            ).get("description")
        )
        self.assertFalse(result(dict(donor, asin="B000000000")).get("description"))
        self.assertEqual(result(dict(donor, description="")).get("description"), None)

    def test_description_recovery_across_configured_sources(self):
        """Retry title-search sources and reject unrelated and blank results."""
        sparse = {
            "source": "asintojson",
            "asin": "B08CK7H7P2",
            "title": "Example (Example Series Book 3)",
            "authors": ["A. Writer"],
            "description": "  ",
            "confidence": 0.99,
        }
        wrong = {
            "source": "first",
            "title": "Example",
            "authors": ["Wrong Writer"],
            "description": "Wrong description",
        }
        empty = {
            "source": "second",
            "title": "Example",
            "authors": ["A. Writer"],
            "description": "  ",
        }
        good = {
            "source": "third",
            "title": "Example",
            "authors": ["A. Writer"],
            "description": "Recovered description",
        }

        def search(provider, *args, **kwargs):
            return {"first": [wrong], "second": [empty], "third": [good]}[provider]

        with patch.object(self.plugin, "_search", side_effect=search) as mocked:
            rows = self.plugin._recover_descriptions(
                self.log,
                self.abort,
                ["asintojson", "first", "second", "third"],
                [sparse],
                sparse["title"],
                sparse["title"],
                ["A. Writer"],
                {"mobi-asin": "B08CK7H7P2"},
                5,
            )
        result = next(row for row in self.plugin._dedupe(rows) if row.get("asin"))
        self.assertEqual(result["description"], "Recovered description")
        self.assertEqual(
            [call.args[0] for call in mocked.call_args_list],
            ["first", "second", "third"],
        )

    def test_description_recovery_reports_unavailable(self):
        """Report exhausted providers and preserve the empty matching result."""
        row = {
            "source": "test",
            "title": "Example",
            "authors": ["A Writer"],
            "description": " \t ",
        }
        with patch.object(self.plugin, "_search", return_value=[]):
            with self.assertLogs(self.log, level="WARNING") as logs:
                result = self.plugin._recover_descriptions(
                    self.log,
                    self.abort,
                    ["test"],
                    [row],
                    "Example",
                    "Example",
                    ["A Writer"],
                    {},
                    5,
                )
        self.assertEqual(result, [row])
        self.assertIn("remains without a description", " ".join(logs.output))

    def test_description_recovery_skips_complete_and_unmatched_results(self):
        """Avoid extra searches when a match is complete or no match exists."""
        complete = {
            "title": "Example",
            "authors": ["A Writer"],
            "description": "Complete",
        }
        other = {"title": "Different", "authors": ["A Writer"], "description": ""}
        with patch.object(self.plugin, "_search") as mocked:
            for row in (complete, other):
                self.plugin._recover_descriptions(
                    self.log,
                    self.abort,
                    ["test"],
                    [row],
                    "Example",
                    "Example",
                    ["A Writer"],
                    {},
                    5,
                )
        mocked.assert_not_called()

    def test_merge_metadata_from_multiple_sources(self):
        """Combine work and exact-edition fields while preserving existing values."""
        primary = {
            "source": "store",
            "source_id": "a",
            "asin": "B012345678",
            "title": "Example",
            "authors": ["A. Writer"],
            "confidence": 1.0,
            "publisher": "Original publisher",
        }
        work = {
            "source": "catalog",
            "source_id": "b",
            "title": "Example",
            "authors": ["A. Writer"],
            "description": "Work description",
            "series": [{"name": "Example Series", "index": 3}],
            "genres": ["Science Fiction"],
            "publisher": "Other edition publisher",
        }
        edition = {
            "source": "identifier",
            "source_id": "c",
            "title": "Example",
            "authors": ["A. Writer"],
            "asin": "B012345678",
            "isbn": "9781234567897",
            "publisher": "Different publisher",
            "published_year": "2021",
            "language": "eng",
        }
        combined = next(
            row
            for row in self.plugin._dedupe([primary, work, edition])
            if row.get("source") == "store"
        )
        self.assertEqual(combined["description"], "Work description")
        self.assertEqual(combined["publisher"], "Original publisher")
        self.assertEqual(combined["published_year"], "2021")
        self.assertEqual(combined["language"], "eng")
        self.assertEqual(combined["isbn"], "9781234567897")
        self.assertIn("Science Fiction", combined["genres"])
        metadata = self.plugin._to_metadata(combined, 0)
        self.assertEqual(
            (metadata.series, metadata.series_index), ("Example Series", 3)
        )
        self.assertNotIn("description", primary)

    def test_work_only_match_does_not_supply_edition_fields(self):
        """A shared title and authors do not establish edition-specific values."""
        base = {
            "source": "store",
            "source_id": "1",
            "title": "Example",
            "authors": ["A Writer"],
            "asin": "B012345678",
            "confidence": 1,
        }
        donor = {
            "source": "catalog",
            "source_id": "2",
            "title": "Example",
            "authors": ["A Writer"],
            "publisher": "Other edition",
            "published_year": "2021",
            "isbn": "9781234567897",
            "description": "Work description",
        }
        result = next(
            row
            for row in self.plugin._dedupe([base, donor])
            if row.get("source") == "store"
        )
        self.assertEqual(result["description"], "Work description")
        self.assertNotIn("publisher", result)
        self.assertNotIn("published_year", result)
        self.assertNotIn("isbn", result)

    def test_conflicting_identifiers_and_series_are_not_merged(self):
        """Never combine conflicting editions or different numbered works."""
        base = {
            "source": "store",
            "source_id": "1",
            "title": "Example",
            "authors": ["A Writer"],
            "isbn": "9781234567897",
            "asin": "B012345678",
            "series": [{"name": "Series", "index": 3}],
            "confidence": 1,
        }
        wrong_id = dict(base, asin="B999999999", description="Wrong edition")
        wrong_series = {
            "source": "other",
            "source_id": "3",
            "title": "Example",
            "authors": ["A Writer"],
            "series": [{"name": "Series", "index": 2}],
            "description": "Different book",
        }
        result = self.plugin._dedupe([base, wrong_id, wrong_series])
        self.assertEqual(len(result), 3)
        original = next(row for row in result if row.get("asin") == "B012345678")
        self.assertNotIn("description", original)

    def test_anthology_matches_partial_author_lists_and_editor(self):
        """Enrich ebook and audiobook results using verified contributor subsets."""
        full = {
            "source": "book",
            "source_id": "1",
            "title": "An Anthology",
            "authors": ["A. Writer", "B. Writer"],
            "contributors": [{"name": "E. Editor", "role": "editor"}],
            "confidence": 1,
        }
        for media in ("books", "audiobooks"):
            for author in ("A. Writer", "E. Editor"):
                with self.subTest(media=media, author=author):
                    donor = {
                        "source": "other",
                        "source_id": "2",
                        "title": "An Anthology",
                        "authors": [author],
                        "metadata_type": media,
                        "description": "Anthology description",
                    }
                    result = next(
                        row
                        for row in self.plugin._dedupe([full, donor])
                        if row["source"] == "book"
                    )
                    self.assertEqual(result["description"], "Anthology description")
                    self.assertEqual(result["authors"], ["A. Writer", "B. Writer"])

    def test_anthology_rejects_conflicting_or_unrelated_contributors(self):
        """A coincidental overlapping author must not connect different lists."""
        base = {
            "source": "a",
            "source_id": "1",
            "title": "An Anthology",
            "authors": ["A Writer", "B Writer"],
            "asin": "B012345678",
        }
        for donor in (
            {"authors": ["A Writer", "C Writer"]},
            {"authors": ["Other Writer"]},
            {"authors": ["A Writer"], "asin": "B999999999"},
            {
                "authors": ["A Writer"],
                "series": [{"name": "Series", "index": 2}],
                "title": "Different Anthology",
            },
        ):
            donor = dict(
                {
                    "source": "b",
                    "source_id": "2",
                    "title": "An Anthology",
                    "description": "Wrong description",
                },
                **donor,
            )
            result = next(
                row
                for row in self.plugin._dedupe([base, donor])
                if row["source"] == "a"
            )
            self.assertFalse(result.get("description"))

    def test_identifier_followup_is_single_hop_and_rejects_wrong_edition(self):
        """Query each compatible source once without traversing newly found IDs."""
        initial = {
            "source": "one",
            "title": "Anthology",
            "authors": ["A Writer"],
            "asin": "B012345678",
        }
        response = [
            {
                "title": "Anthology",
                "authors": ["A Writer"],
                "asin": "B012345678",
                "isbn": "9781234567897",
                "description": "Matched",
            },
            {
                "title": "Anthology",
                "authors": ["A Writer"],
                "asin": "B999999999",
                "description": "Wrong edition",
            },
        ]
        with patch.object(self.plugin, "_search", return_value=response) as search:
            rows = self.plugin._expand_identifiers(
                self.log,
                self.abort,
                ["asintojson", "isbnlab", "open_library"],
                [initial],
                "Anthology",
                "Anthology",
                ["A Writer"],
                {},
                5,
            )
        self.assertEqual(
            [call.args[0] for call in search.call_args_list], ["asintojson", "isbnlab"]
        )
        self.assertTrue(
            all(
                call.args[3] == {"asin": "B012345678"} for call in search.call_args_list
            )
        )
        self.assertTrue(all(row.get("asin") != "B999999999" for row in rows))
        self.assertEqual(len(rows), 3)

    def test_identifier_followup_uses_discovered_isbn_without_repeating_asin(self):
        """An exact ASIN match can seed an ISBN lookup without repeating the ASIN."""
        initial = {
            "source": "one",
            "title": "Anthology",
            "authors": ["A Writer"],
            "asin": "B012345678",
            "isbn": "9781234567897",
        }
        with patch.object(self.plugin, "_search", return_value=[]) as search:
            self.plugin._expand_identifiers(
                self.log,
                self.abort,
                ["asintojson", "open_library"],
                [initial],
                "Anthology",
                "Anthology",
                ["A Writer"],
                {"mobi-asin": "B012345678"},
                5,
            )
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.args[0], "open_library")
        self.assertEqual(search.call_args.args[3], {"isbn": "9781234567897"})

    def test_multi_author_lookup_tries_individual_contributors(self):
        """Recover an anthology listed by one contributor when a combined query misses."""
        self.plugin.prefs["plugin_ids"] = "hardcover"

        def lookup(provider, query, authors, identifiers, timeout, **kwargs):
            if authors == ["B Writer"]:
                return [
                    {
                        "source": "hardcover",
                        "source_id": "result",
                        "title": "An Anthology",
                        "authors": ["B Writer"],
                        "description": "Anthology synopsis",
                        "confidence": 0.9,
                    }
                ]
            return []

        with patch.object(self.plugin, "_search", side_effect=lookup) as search:
            results = self.identify(
                title="An Anthology", authors=["A Writer", "B Writer", "C Writer"]
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].comments, "Anthology synopsis")
        self.assertEqual(
            [call.args[2] for call in search.call_args_list],
            [["A Writer", "B Writer", "C Writer"], None, ["A Writer"], ["B Writer"]],
        )

    def test_same_identifier_with_conflicting_title_or_authors_is_rejected(self):
        """A source's reused ISBN or ASIN must not override contradictory evidence."""
        primary = {
            "title": "Example",
            "authors": ["A Writer"],
            "isbn": "9781234567897",
            "source": "a",
            "source_id": "1",
            "confidence": 1,
        }
        for changed in ({"title": "Unrelated"}, {"authors": ["Other Writer"]}):
            donor = dict(primary, source="b", source_id="2", description="Wrong")
            donor.update(changed)
            rows = self.plugin._dedupe([primary, donor])
            self.assertEqual(len(rows), 2)
            self.assertFalse(rows[0].get("description"))

    def test_edition_details_are_corroborating_not_standalone_identity(self):
        """Two large same-format discrepancies prevent an unsafe edition merge."""
        base = {
            "title": "Example",
            "authors": ["A Writer"],
            "isbn": "9781234567897",
            "published_year": "2000",
            "extra": {"format": "paperback", "page_count": 200},
        }
        changed = dict(
            base,
            published_year="2015",
            extra={"format": "paperback", "page_count": 600},
        )
        self.assertFalse(self.plugin._identity_consistent(base, changed))
        one_discrepancy = dict(changed, published_year="2000")
        self.assertTrue(self.plugin._identity_consistent(base, one_discrepancy))
        other_format = dict(changed, extra={"format": "audiobook", "page_count": 600})
        self.assertTrue(self.plugin._identity_consistent(base, other_format))

    def test_title_typography_matches_without_removing_words(self):
        """Normalize curly apostrophes and dashes without conflating different words."""
        original = {"title": "The Writer’s Book", "authors": ["A Writer"]}
        equivalent = {"title": "The Writer's Book", "authors": ["A Writer"]}
        wrong = {"title": "The Writers Book", "authors": ["A Writer"]}
        self.assertTrue(self.plugin._work_description_matches(original, equivalent))
        self.assertFalse(self.plugin._work_description_matches(original, wrong))

    def test_catalog_ids_are_preserved_but_dewey_is_not_an_identity(self):
        """Expose source identifiers while never merging on a classification number."""
        book = {
            "title": "One",
            "authors": ["A Writer"],
            "source": "library",
            "extra": {
                "lccn": "2025001234",
                "oclc": "123456",
                "dewey_decimal": "813.54",
                "goodreads_id": "12345",
                "open_library_edition_id": "OL123M",
            },
        }
        result = self.plugin._to_metadata(book, 0).get_identifiers()
        for kind, value in (
            ("lccn", "2025001234"),
            ("oclc", "123456"),
            ("dewey", "813.54"),
            ("goodreads", "12345"),
            ("openlibrary", "OL123M"),
        ):
            self.assertEqual(result[kind], value)
        unrelated = dict(
            book, title="Two", source="other", extra={"dewey_decimal": "813.54"}
        )
        self.assertFalse(self.plugin._same_edition(book, unrelated))

    def test_catalog_id_followups_are_verified_and_not_recursive(self):
        """Follow Goodreads and LCCN IDs while rejecting misleading records."""
        rows = [
            {
                "title": "Example",
                "authors": ["A Writer"],
                "source": "base",
                "extra": {"goodreads_id": "12345", "lccn": "2025001234"},
            }
        ]

        def lookup(provider, query, authors, identifiers, timeout, **kwargs):
            if provider == "goodreads":
                return [
                    {
                        "title": "Wrong",
                        "authors": ["A Writer"],
                        "source": "goodreads",
                        "source_id": "12345",
                        "extra": {"goodreads_id": "12345"},
                    }
                ]
            return [
                {
                    "title": "Example",
                    "authors": ["A Writer"],
                    "source": "loc_json_yaml",
                    "extra": {"lccn": "2025001234", "oclc": "new-id"},
                }
            ]

        with patch.object(self.plugin, "_search", side_effect=lookup) as search:
            output = self.plugin._expand_identifiers(
                self.log,
                self.abort,
                ["goodreads", "loc_json_yaml"],
                rows,
                "Example",
                "Example",
                ["A Writer"],
                {},
                5,
            )
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(output), 2)
        self.assertTrue(all(row["title"] == "Example" for row in output))

    def test_isbn_checksum_and_equivalent_formats(self):
        """Reject invalid ISBNs and recognize equivalent ISBN-10 and ISBN-13."""
        self.assertEqual(self.plugin._canonical_isbn("0-306-40615-2"), "9780306406157")
        self.assertEqual(
            self.plugin._canonical_isbn("978-0-306-40615-7"), "9780306406157"
        )
        self.assertFalse(self.plugin._canonical_isbn("9780306406158"))
        left = {"title": "Example", "isbn": "0306406152"}
        same = {"title": "Example", "isbn": "9780306406157"}
        self.assertTrue(self.plugin._same_edition(left, same))
        invalid = {"title": "Example", "isbn": "9780306406158"}
        self.assertFalse(self.plugin._same_edition(invalid, invalid))
        self.assertFalse(self.plugin._canonical_isbn("4006381333931"))
        self.assertNotIn("isbn", self.plugin._to_metadata(invalid, 0).get_identifiers())
        winner = {
            "title": "Example",
            "authors": ["A Writer"],
            "asin": "B012345678",
            "isbn": "9780306406158",
        }
        donor = dict(winner, isbn="9780306406157")
        self.assertTrue(self.plugin._merge_metadata(winner, donor))
        self.assertEqual(winner["isbn"], "9780306406157")

    def test_open_library_work_id_lookup_checks_work_and_edition(self):
        """A work ID remains usable even when the response also has an edition ID."""
        result = {
            "title": "Example",
            "authors": ["A Writer"],
            "source": "open_library",
            "source_id": "OL123M",
            "extra": {
                "open_library_work_id": "OL456W",
                "open_library_edition_id": "OL123M",
            },
        }
        with patch.object(self.plugin, "_search", return_value=[result]) as search:
            rows = self.plugin._expand_identifiers(
                self.log,
                self.abort,
                ["open_library"],
                [],
                "Example",
                "Example",
                ["A Writer"],
                {"openlibrary": "OL456W"},
                5,
            )
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.args[1], "OL456W")
        self.assertEqual(rows, [result])

    def test_lccn_and_oclc_resolve_through_open_library(self):
        """Follow only exact source-verified library catalog identifiers."""
        for scheme, value in (("lccn", "93005405"), ("oclc", "28419896")):
            with self.subTest(scheme=scheme):
                result = {
                    "source": "open_library",
                    "title": "Zen speaks",
                    "authors": ["Zhizhong Cai"],
                    "extra": {scheme: value},
                }
                with patch.object(
                    self.plugin, "_search", return_value=[result]
                ) as search:
                    rows = self.plugin._expand_identifiers(
                        self.log,
                        self.abort,
                        ["open_library"],
                        [],
                        "Zen speaks",
                        "Zen speaks",
                        ["Zhizhong Cai"],
                        {scheme: value},
                        5,
                    )
                self.assertEqual(search.call_args.args[1], scheme.upper() + ":" + value)
                self.assertEqual(rows, [result])

    def test_identifier_followup_has_request_budget(self):
        """Bound provider calls even when a book has several identifiers."""
        row = {
            "source": "origin",
            "title": "Example",
            "authors": ["A Writer"],
            "isbn": "9780306406157",
            "asin": "B012345678",
            "extra": {"goodreads_id": "12345", "lccn": "93005405"},
        }
        providers = [
            "isbnlab",
            "goodreads",
            "google_books",
            "hardcover",
            "open_library",
            "internet_archive",
            "new_york_times_books",
            "asintojson",
            "audible",
            "audnexus",
            "loc_json_yaml",
        ]
        with patch.object(self.plugin, "_search", return_value=[]) as search:
            with self.assertLogs(self.log, level="WARNING") as logs:
                self.plugin._expand_identifiers(
                    self.log,
                    self.abort,
                    providers,
                    [row],
                    "Example",
                    "Example",
                    ["A Writer"],
                    {},
                    5,
                )
        self.assertEqual(search.call_count, 12)
        self.assertIn("stopped at 12 requests", " ".join(logs.output))

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
    if result.testsRun != 50:
        raise RuntimeError("The real-Calibre regression suite was not fully collected")
    sys.exit(0 if result.wasSuccessful() else 1)
