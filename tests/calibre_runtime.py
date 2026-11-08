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
from threading import Barrier, Event
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError
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
        # These fixture tests define their enabled test inventory through the
        # local provider preference; selection policy has independent coverage.
        discover = self.plugin._discover

        def fixture_inventory(timeout, abort):
            return self.plugin._configured_provider_ids() or discover(timeout, abort)

        inventory = patch.object(
            self.plugin, "_discover", side_effect=fixture_inventory
        )
        inventory.start()
        self.addCleanup(inventory.stop)
        self.log = logging.getLogger("calibre-validation")
        self.abort = Event()

    def identify(self, **kwargs):
        """Invoke Calibre's metadata source interface and drain its results."""
        queue = Queue()
        error = self.plugin.identify(self.log, queue, self.abort, **kwargs)
        self.assertIsNone(error)
        return list(queue.queue)

    def test_catalog_extent_triggers_real_search_path_recovery(self):
        """Recover meaningful prose after catalog-only initial results, not a stubbed search."""
        title = "Spice travels : a spice merchant's voyage of discovery"
        synopsis = "Ian Hemphill follows the spice trade, exploring growers, markets and the origins of familiar seasonings."
        requests = []
        self.plugin.prefs["plugin_ids"] = "google_books,internet_archive,open_library"

        def api(url, timeout):
            parsed = urlsplit(url)
            provider = parsed.path.split("/")[-2]
            args = parse_qs(parsed.query)
            requests.append((provider, args))
            row = {
                "title": title,
                "authors": ["Ian Rupert Hemphill"],
                "isbn": "0732911516",
                "source_id": "fixture",
                "description": "1 volume",
                "genres": ["Condiments"],
            }
            if provider == "google_books":
                row["title"] = "Spice Travels"
                if "author" not in args:
                    row["description"] = synopsis
            elif provider == "internet_archive":
                row["authors"] = ["1949- Hemphill Ian R. (Ian Rupert)"]
                row["genres"] = ["Spice trade"]
            elif provider == "open_library":
                row["authors"] = ["Ian R. Hemphill"]
                row["description"] = "227 p. :"
            else:
                return {"results": []}
            return {"results": [row]}

        with patch.object(self.plugin, "_http_json", side_effect=api):
            results = self.identify(title=title, authors=["Ian Rupert"], identifiers={"isbn": "0732911516"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].authors, ["Ian Rupert Hemphill"])
        self.assertEqual(results[0].comments, synopsis)
        self.assertIn("Spice Trade", results[0].tags)
        google = [args for provider, args in requests if provider == "google_books"]
        self.assertEqual(len(google), 2)
        self.assertIn("author", google[0])
        self.assertNotIn("author", google[1])

    def test_extent_variants_and_short_real_descriptions(self):
        """Reject physical catalog notes while preserving short genuine summaries."""
        for value in ("1 volume", "2 volumes", "1 v.", "one volume", "1 volume (various pagings)", "227 p. :", "<p>&nbsp;</p>"):
            with self.subTest(value=value):
                self.assertIsNone(self.plugin._usable_description(value))
        for value in ("A journey through spice markets.", "One volume changes her life."):
            self.assertEqual(self.plugin._usable_description(value), value)

    def test_transport_timeout_retries_once_without_retrying_http_failures(self):
        """Bound timeout recovery at the transport boundary used by all requests."""
        with patch.object(importlib.import_module(self.plugin.__class__.__module__), "urlopen", side_effect=URLError(TimeoutError("timed out"))) as opening:
            with self.assertRaises(RuntimeError):
                self.plugin._http_bytes(self.url + "/timeout", 2)
        self.assertEqual(opening.call_count, 2)

    def test_truncated_catalog_author_requires_corroborated_edition_evidence(self):
        """Recover the reported Spice Travels lookup without trusting a lone ID."""
        title = "Spice travels : a spice merchant's voyage of discovery"
        google = {
            "title": "Spice Travels",
            "authors": ["Ian Rupert Hemphill"],
            "isbn": "9780732911515",
            "source": "google_books",
            "description": "A spice merchant explores the origins and uses of spices.",
            "genres": ["Condiments"],
        }
        archive = {
            "title": title,
            "authors": ["1949- Hemphill Ian R. (Ian Rupert)"],
            "isbn": "0732911516",
            "source": "internet_archive",
            "description": "227 p. :",
            "genres": ["Spice trade"],
            "tags": ["internet-archive", "archive.org", "inlibrary", "printdisabled"],
        }
        library = dict(google, authors=["Ian R. Hemphill"], source="open_library")
        inventory = {
            "google_books": [google],
            "internet_archive": [archive],
            "open_library": [library],
        }
        self.plugin.prefs["plugin_ids"] = ",".join(inventory)

        def search(provider, *args, **kwargs):
            return [dict(row) for row in inventory.get(provider, [])]

        with patch.object(self.plugin, "_search", side_effect=search):
            results = self.identify(
                title=title,
                authors=["Ian Rupert"],
                identifiers={"isbn": "0732911516"},
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].authors, ["Ian Rupert Hemphill"])
        self.assertEqual(results[0].comments, google["description"])
        self.assertIn("Condiments", results[0].tags)
        self.assertIn("Spice Trade", results[0].tags)
        self.assertNotIn("archive.org", results[0].tags)
        self.assertNotIn("Inlibrary", results[0].tags)
        self.assertNotIn("Printdisabled", results[0].tags)
        self.assertIn("voyage of discovery", results[0].title.casefold())
        for rows in ([google], [google, dict(library, source="google_books")]):
            self.assertEqual(
                self.plugin._recover_incomplete_author(
                    self.log, rows, title, ["Ian Rupert"], {"isbn": "0732911516"}
                ),
                ["Ian Rupert"],
            )
        self.assertEqual(
            self.plugin._recover_incomplete_author(
                self.log,
                [google, archive],
                title,
                ["Ian Someone"],
                {"isbn": "0732911516"},
            ),
            ["Ian Someone"],
        )
        conflict = dict(google, authors=["Ian Robert Hemphill"])
        self.assertFalse(self.plugin._identity_consistent(google, conflict))
        subtitle = dict(archive, title="Spice travels: A different book")
        self.assertFalse(self.plugin._identity_consistent(archive, subtitle))

    def test_series_suffix_is_removed_before_initial_parallel_search(self):
        """Find the Eli Wray book and reject unrelated sugar-and-spice titles."""
        title = "Sugar And Spice (A Very Enby Christmas Book 1)"
        authors = ["Eli Wray"]
        asin = "B08JCS4TNZ"
        book = {
            "title": "Sugar and Spice (A Very Enby Christmas #1)",
            "authors": authors,
            "source": "goodreads",
            "source_id": "55386622",
            "series": [],
            "description": "Mason tries baking cookies for Natalie.",
            "genres": ["Romance", "Queer", "Holiday"],
        }
        wrong = {
            "title": "Sugar and Spice 'n' Everything Nice: Holiday Recipes",
            "authors": ["Woman's Guild"],
            "source": "google_books",
        }

        def search(provider, query, *args, **kwargs):
            if provider == "google_books":
                return [dict(wrong)]
            if query == "Sugar And Spice" or query == "goodreads:55386622":
                return [dict(book)]
            return []

        self.plugin.prefs["plugin_ids"] = "google_books,goodreads"
        with patch.object(self.plugin, "_search", side_effect=search) as calls:
            results = self.identify(
                title=title, authors=authors, identifiers={"mobi-asin": asin}
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Sugar and Spice")
        self.assertEqual(results[0].authors, authors)
        self.assertEqual(results[0].series, "A Very Enby Christmas")
        self.assertEqual(results[0].series_index, 1)
        self.assertEqual(results[0].comments, book["description"])
        self.assertEqual(results[0].tags, book["genres"])
        initial = calls.call_args_list[:2]
        self.assertEqual({call.args[1] for call in initial}, {"Sugar And Spice"})

    def test_initial_provider_requests_overlap_for_metadata_and_covers(self):
        """A barrier must admit every provider before any initial call completes."""
        providers = ["google_books", "goodreads", "open_library"]
        for operation in ("identify", "cover"):
            with self.subTest(operation=operation):
                barrier = Barrier(len(providers), timeout=3)

                def search(provider, query, *args, **kwargs):
                    barrier.wait()
                    return []

                with (
                    patch.object(
                        self.plugin, "_book_provider_ids", return_value=providers
                    ),
                    patch.object(self.plugin, "_search", side_effect=search) as calls,
                ):
                    if operation == "identify":
                        self.assertEqual(self.identify(title="Unknown Book"), [])
                    else:
                        queue = Queue()
                        self.plugin.download_cover(
                            self.log, queue, self.abort, title="Unknown Book"
                        )
                        self.assertTrue(queue.empty())
                self.assertFalse(barrier.broken)
                self.assertEqual(calls.call_count, len(providers))

    def test_specter_without_identifier_combines_partial_sources(self):
        """A title and authors lookup must return one complete leading match."""
        authors = ["M. D. Cooper", "Chris J. Pike", "Jill Cooper"]
        amazon = {
            "title": "A Specter and an Invasion",
            "authors": [],
            "series": [{"name": "Legacy of the Lost", "position": 3}],
            "asin": "B08CK7H7P2",
            "genres": [
                "Kindle Store",
                "Kindle eBooks",
                "Science Fiction & Fantasy",
                "Science Fiction",
                "Space Opera",
            ],
            "source": "amazon",
            "confidence": 0.99,
            "metadata_type": "book",
        }
        goodreads = {
            "title": "A Specter and an Invasion (Legacy of the Lost #3)",
            "authors": ["Chris J. Pike", "M. D. Cooper", "Jill Cooper"],
            "source": "goodreads",
            "source_id": "55033599",
            "series": [],
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
        self.assertEqual(
            len(results), 1, "Matching provider records must form one result"
        )
        match = results[0]
        self.assertEqual(
            match.tags, ["Science Fiction & Fantasy", "Science Fiction", "Space Opera"]
        )
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
            ["Paranormal Romance"],
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
            "tags": [
                "goodreads",
                "audiobooks",
                "metadata-engine:goodreads",
                "Cleaned",
                "Open-Library",
                "open_library",
                "OPEN LIBRARY",
                "Google-Books",
                "Open Libraries",
            ],
            "description": "Source: Goodreads\nTime has passed. Goodreads is mentioned in prose.",
        }
        metadata = self.plugin._to_metadata(row, 0)
        self.assertEqual(
            metadata.tags,
            [
                "Science Fiction & Fantasy",
                "Science Fiction",
                "Space Opera",
                "Open Libraries",
            ],
        )
        self.assertEqual(
            metadata.comments, "Time has passed. Goodreads is mentioned in prose."
        )

    def test_book_provider_inventory_overrides_an_old_restricted_list(self):
        """Select every enabled book provider regardless of saved restrictions."""
        self.plugin.prefs["plugin_ids"] = "goodreads"
        payload = {
            "plugins": [
                {"plugin_id": "goodreads", "metadata_types": ["books"]},
                {"plugin_id": "open_library", "metadata_types": ["books"]},
                {
                    "plugin_id": "disabled",
                    "enabled": False,
                    "metadata_types": ["books"],
                },
                {"plugin_id": "tvmaze", "metadata_types": ["tv"]},
            ]
        }
        with patch.object(self.plugin, "_http_json", return_value=payload):
            selected = type(self.plugin)._discover(self.plugin, 5, self.abort)
        self.assertEqual(selected, ["goodreads", "open_library"])
        with patch.object(self.plugin, "_discover", return_value=selected):
            self.assertEqual(
                self.plugin._book_provider_ids(self.log, 5, self.abort),
                ["google_books", "goodreads", "open_library"],
            )

    def test_sparse_goodreads_result_is_enriched_by_its_own_identifier(self):
        """A match is incomplete until its missing fields have been checked by ID."""
        title = "Homemade Sauce & Dip Recipes"
        authors = ["Tyler Sweet"]
        sparse = {
            "title": title,
            "authors": authors,
            "source": "goodreads",
            "source_id": "58987091",
            "extra": {"goodreads_id": "58987091"},
        }
        detailed = dict(sparse, description="A collection of homemade sauces and dips.")
        self.plugin.prefs["plugin_ids"] = "goodreads"

        def search(provider, query, *args, **kwargs):
            if provider != "goodreads":
                return []
            return [dict(detailed if query == "goodreads:58987091" else sparse)]

        with (
            patch.object(self.plugin, "_search", side_effect=search) as lookup,
            self.assertLogs(self.log, level="INFO") as logs,
        ):
            results = self.identify(title=title, authors=authors)
        self.assertEqual(results[0].comments, detailed["description"])
        self.assertEqual(results[0].tags, [])
        self.assertEqual(
            sum(call.args[1] == "goodreads:58987091" for call in lookup.call_args_list),
            1,
        )
        self.assertIn("incomplete matching result: tags", " ".join(logs.output))
        detailed["tags"] = ["Cooking", "Recipes"]
        with patch.object(self.plugin, "_search", side_effect=search):
            results = self.identify(title=title, authors=authors)
        self.assertEqual(results[0].tags, ["Cooking", "Recipes"])
        self.assertEqual(results[0].comments, detailed["description"])

    def test_missing_subjects_are_filled_without_replacing_comments(self):
        """Use only the matching ISBN's catalog subjects in Calibre."""
        title = "The Naughty Couple's Bucket List"
        isbn = "9798887680118"
        authors = ["Nicole Jones"]
        row = {
            "title": title,
            "isbn": isbn,
            "authors": authors,
            "description": "Existing synopsis",
            "source": "google_books",
            "confidence": 0.99,
        }

        def search(provider, *args, **kwargs):
            if provider == "google_books":
                return [dict(row)]
            return [
                dict(row, isbn="9781452163857", tags=["Wrong edition"]),
                dict(row, tags=["Sexuality", "Sacred Sexuality", "Sexual Instruction"]),
            ]

        self.plugin.prefs["plugin_ids"] = "google_books"
        with patch.object(self.plugin, "_search", side_effect=search) as lookup:
            result = self.identify(
                title=title, authors=authors, identifiers={"isbn": isbn}
            )
        self.assertEqual(
            [call.args[0] for call in lookup.call_args_list],
            ["google_books", "tertulia_subjects"],
        )
        self.assertEqual(result[0].comments, "Existing synopsis")
        self.assertEqual(
            result[0].tags, ["Sexuality", "Sacred Sexuality", "Sexual Instruction"]
        )

    def test_catalog_tag_periods_are_removed_before_deduplication(self):
        """Split subject paths, discard terminal periods, and deduplicate."""
        row = {
            "title": "Herbs & Spices",
            "genres": ["Cookbooks.", "Spices.", "Herbs.", "Herb Gardening."],
            "tags": [
                "cookbooks",
                "Spices",
                "Herbs",
                "Herb Gardening",
                "Cooking (Spices)",
                "U.S. Cooking.",
                "Cooking",
                "COOKING / Reference. / Herbs",
                "GARDENING / Herbs",
                "https://archive.org/subject/example",
                "Open-Library.",
                ".",
            ],
        }
        metadata = self.plugin._to_metadata(row, 0)
        self.assertEqual(
            metadata.tags,
            [
                "Cookbooks",
                "Spices",
                "Herbs",
                "Herb Gardening",
                "Cooking (Spices)",
                "U.S. Cooking",
                "Cooking",
                "Reference",
                "GARDENING",
            ],
        )

    def test_catalog_classifications_are_not_calibre_tags(self):
        """Drop format and maturity markers even from cached provider rows."""
        row = {
            "title": "Example",
            "source": "google_books",
            "tags": ["books", "not_mature"],
        }
        self.assertEqual(self.plugin._to_metadata(row, 0).tags, [])
        row["genres"] = ["Space Opera", "Mature Themes", "Books About Books"]
        row["tags"] += ["NOT-MATURE", "mature", "space opera"]
        metadata = self.plugin._to_metadata(row, 0)
        self.plugin.clean_downloaded_metadata(metadata)
        self.assertEqual(
            metadata.tags, ["Space Opera", "Mature Themes", "Books About Books"]
        )

    def test_subjects_deduplicated_after_calibre_cleanup(self):
        """Collapse mixed-case subject duplicates without discarding distinct subjects."""
        row = {
            "title": "Tired as F*ck",
            "authors": ["Caroline Dooner"],
            "source": "open_library",
            "genres": ["Books", "Psychology", "Self-Improvement"],
            "tags": [
                "books",
                " BOOKS ",
                "psychology",
                "Open-Library",
                "Cleaned",
                "Self Improvement",
                "Health, Mind & Body",
            ],
        }
        metadata = self.plugin._to_metadata(row, 0)
        self.plugin.clean_downloaded_metadata(metadata)
        normalized = [" ".join(tag.casefold().split()) for tag in metadata.tags]
        self.assertNotIn("books", normalized, repr(metadata.tags))
        self.assertEqual(normalized.count("psychology"), 1, repr(metadata.tags))
        self.assertIn("Self-Improvement", metadata.tags)
        self.assertIn("Self Improvement", metadata.tags)
        self.assertNotIn("Open-Library", metadata.tags)
        self.assertNotIn("Cleaned", metadata.tags)

    def test_saved_kindle_asin_does_not_block_goodreads_isbn(self):
        """Only send the saved Kindle identifier to ASIN-capable sources."""
        from urllib.parse import parse_qs, urlsplit

        identifiers = {"isbn": "9781977552136", "mobi-asin": "B075JB53VN"}
        with patch.object(
            self.plugin, "_http_json", return_value={"results": []}
        ) as request:
            for provider in ("goodreads", "amazon"):
                self.plugin._search(
                    provider,
                    "Fairy Rings and Dragon Kings",
                    ["Amy Sumida"],
                    identifiers,
                    30,
                )
        goodreads, amazon = [
            parse_qs(urlsplit(call.args[0]).query) for call in request.call_args_list
        ]
        self.assertEqual(goodreads["isbn"], ["9781977552136"])
        self.assertNotIn("asin", goodreads)
        self.assertEqual(amazon["asin"], ["B075JB53VN"])

    def test_description_lists_render_as_native_calibre_html(self):
        """Preserve numbering, paragraph boundaries, and literal punctuation."""
        from calibre.library.comments import comments_to_html

        description = (
            "Getting started\n\n3. Warm up\n4) Run & walk\n\n"
            "Bring along:\n- Water\n• Shoes\n\nThen cool down."
        )
        row = {"title": "Example", "description": description}
        comments = comments_to_html(self.plugin._to_metadata(row, 0).comments)
        self.assertIn(
            '<ol start="3"><li>Warm up</li><li>Run &amp; walk</li></ol>', comments
        )
        self.assertIn("<ul><li>Water</li><li>Shoes</li></ul>", comments)
        self.assertTrue(comments.endswith("<p>Then cool down.</p>"))

    def test_catalog_contents_are_separate_from_the_abstract(self):
        """Render the reported flattened contents without turning prose into a list."""
        description = (
            "Foreword; Introduction; Chapter 1: Get Moving!; "
            "Chapter 2: It Starts with a Single Step; "
            "Appendix A: Resources; Acknowledgments; Index.;Founded on a simple idea."
        )
        comments = self.plugin._format_description(description)
        self.assertIn("<ul><li>Foreword</li><li>Introduction</li></ul>", comments)
        self.assertIn(
            "<ol><li>Get Moving!</li><li>It Starts with a Single Step</li></ol>",
            comments,
        )
        self.assertTrue(comments.endswith("<p>Founded on a simple idea.</p>"))
        for unchanged in (
            "There were three steps; all were simple.",
            "<p>Existing HTML</p><ul><li>Water</li></ul>",
            "2026. A good year.\nA new paragraph.",
        ):
            self.assertEqual(self.plugin._format_description(unchanged), unchanged)

    def test_verified_isbn_with_synopsis_beats_conflicting_archive_author(self):
        """Rank the complete, corroborated edition above the malformed record."""
        isbn = "9781465435989"
        title = "Herbs & Spices"
        synopsis = "An illustrated guide to herbs and spices, with recipes."
        archive = {
            "title": title,
            "authors": ["1940- Author Norman Jillian"],
            "isbn": isbn,
            "source": "internet_archive",
            "confidence": 0.99,
        }
        catalog = dict(
            archive,
            authors=["Jill Norman", "Dave King", "Inc. Dk Publishing"],
            source="loc_sru_z3950",
            confidence=0.85,
            description=synopsis,
        )
        self.plugin.prefs["plugin_ids"] = "internet_archive,loc_sru_z3950"
        rows = {
            "google_books": [],
            "internet_archive": [archive],
            "loc_sru_z3950": [catalog],
        }

        def search(provider, *args, **kwargs):
            return [dict(row) for row in rows[provider]]

        with patch.object(self.plugin, "_search", side_effect=search):
            results = self.identify(
                title=title, authors=["Jill Norman"], identifiers={"isbn": isbn}
            )
        self.assertEqual(results[0].comments, synopsis)
        self.assertEqual(results[0].authors[0], "Jill Norman")
        self.assertEqual(len(results), 1)

    def test_physical_description_does_not_block_matching_catalog_synopsis(self):
        """Merge the reported ISBN and never display bare physical details."""
        archive = {
            "title": "The Spice Routes : Chronicles and Recipes From Around the World",
            "authors": ["Chris Caldicott"],
            "isbn": "0711217564",
            "description": "192 p. : 25 cm",
            "source": "internet_archive",
            "confidence": 0.99,
        }
        synopsis = "A travelogue and cookbook following the old spice trade routes."
        catalog = {
            "title": "The Spice Routes: Chronicles and Recipes From Around the World",
            "authors": ["Chris. Caldicott", "Carolyn. Caldicott"],
            "isbn": "9780711217560",
            "description": synopsis,
            "source": "loc_sru_z3950",
            "confidence": 0.95,
        }
        unrelated = dict(catalog, title="Another cookbook", description="Wrong book.")
        for placeholder in ("192 p. : 25 cm", "1108 pages : 26 cm", "<p>&nbsp;</p>"):
            row = dict(archive, description=placeholder)
            self.assertFalse(self.plugin._to_metadata(row, 0).comments)
            results = self.plugin._dedupe([row, unrelated, catalog])
            self.assertEqual(len(results), 2)
            self.assertEqual(self.plugin._to_metadata(results[0], 0).comments, synopsis)
        valid = "192 pages of recipes and travel stories."
        results = self.plugin._dedupe([dict(archive, description=valid), catalog])
        self.assertEqual(results[0]["description"], valid)

    def test_catalog_parts_render_nested_contents_in_calibre(self):
        """Preserve numbered parts and their unnumbered entries from the catalog."""
        from calibre.library.comments import comments_to_html

        description = (
            "Acknowledgements -- Preface -- Introduction -- Pt. 1: Elements Of "
            "Magic Spells: Spell-casting -- Key Concepts For Casting Magic Spells "
            "-- Ritual Tools And Techniques -- Using This Book -- Pt. 2: The "
            "Spells: Animal Spells -- Banishing Spells -- Better Business And "
            "Professional Success Spells -- Cleansing Spells -- Courtcase Spells: "
            "Legal Spells And Spells For Justice -- Death Spells: Death, Ghosts, "
            "Necromancy, And Vampires -- Divination Spells -- Spells Of "
            "Domination, Persuasion, And Influence -- Dream Spells: Dreams, "
            "Insomnia, Nightmares, Astral Projection, And Witches' Flying Potions "
            "-- The Evil Eye -- Fertility: Spells For Conception And Contraception "
            "-- Fire Safety Spells -- Gamblers' Spells And Charms -- Happy Home "
            "Spells -- Healing Spells -- Hexes And Their Antidotes -- Invisibility "
            "And Transformation Spells -- Love Magic: Spells For Romance, True "
            "Love, Seduction And Sex -- Luck And Success Spells -- Marriage And "
            "Divorce Spells -- Money Spells : Spells For Wealth, Prosperity, And "
            "Financial Stability -- Pregnancy And Childbirth Spells: Spells To "
            "Protect Mothers And Infants -- Protection Spells -- Psychic Power "
            "Spells -- Spells For Travelers -- Spirit Summoning Spells -- Theft, "
            "Lost Objects, And Missing Persons -- Unblocking Spells -- Weather "
            "Spells -- Youth, Beauty, And Longevity Spells -- Formulary -- Tables "
            "-- Bibliography -- Botanical Classificaitons -- Index. Judika Illes. "
            "Subtitle From Cover."
        )
        row = {
            "title": "The Element Encyclopedia of 5000 Spells",
            "description": description,
        }
        comments = comments_to_html(self.plugin._to_metadata(row, 0).comments)
        self.assertIn("<ul><li>Acknowledgements</li><li>Preface</li>", comments)
        self.assertIn(
            "<ol><li>Elements Of Magic Spells<ul><li>Spell-casting</li>", comments
        )
        self.assertIn("</ul></li><li>The Spells<ul><li>Animal Spells</li>", comments)
        self.assertIn("<li>Index</li></ul></li></ol>", comments)
        self.assertTrue(comments.endswith("<p>Judika Illes. Subtitle From Cover.</p>"))
        prose = "It looked promising -- I thought so -- until it fell apart."
        self.assertEqual(self.plugin._format_description(prose), prose)

    def test_separate_subtitle_does_not_lose_same_edition_tags(self):
        """Merge the matching full title without merging an unrelated ISBN claim."""
        title = "Design Multi-Agent AI Systems Using MCP and A2A"
        subtitle = (
            "Engineer your own Python-based agentic AI framework with tool use, "
            "memory, and multi-agent workflows"
        )
        isbn = "9781806116478"
        winner = {
            "title": f"{title}: {subtitle}",
            "authors": ["Gigi Sayfan"],
            "isbn": isbn,
            "source": "goodreads",
            "confidence": 0.995,
            "description": "Build and deploy a multi-agent AI framework.",
        }
        donor = {
            "title": title,
            "subtitle": subtitle.title(),
            "authors": ["Gigi. Sayfan"],
            "isbn": isbn,
            "source": "google_books",
            "confidence": 0.99,
            "genres": ["Computers"],
            "tags": ["Programming", "Computers & Internet", "google-books"],
        }
        unrelated = dict(donor, subtitle="An unrelated subtitle", tags=["Unrelated"])
        rows = self.plugin._dedupe([winner, donor, unrelated])
        self.assertEqual(len(rows), 2)
        result = self.plugin._to_metadata(rows[0], 0)
        self.assertEqual(result.title, winner["title"])
        self.assertEqual(result.authors, ["Gigi Sayfan"])
        self.assertEqual(result.comments, winner["description"])
        self.assertEqual(
            result.tags, ["Computers", "Programming", "Computers & Internet"]
        )

    def test_requested_series_prefix_requires_edition_and_catalog_evidence(self):
        """Recover an explicit series position only for a corroborated edition."""
        title = "Blossomverse Omegaverse 1: Feeling the Heat: Part One"
        authors = ["Emily Antoinette"]
        ids = {"isbn": "9781967556113"}
        row = {
            "title": "Feeling the Heat: Part One (Blossomverse Omegaverse)",
            "authors": authors,
            "isbn": ids["isbn"],
        }
        enriched = self.plugin._with_requested_series(row, title, authors, ids)
        self.assertEqual(
            enriched["series"], [{"name": "Blossomverse Omegaverse", "position": 1}]
        )
        for wrong in (
            dict(row, authors=["Other Author"]),
            dict(row, isbn="9781967556120"),
            dict(row, title="Feeling the Heat: Part Two (Blossomverse Omegaverse)"),
            dict(row, title="Feeling the Heat: Part One (Other Series)"),
        ):
            self.assertNotIn(
                "series", self.plugin._with_requested_series(wrong, title, authors, ids)
            )
        self.assertNotIn("series", row)

    def test_numbered_volume_fills_missing_series(self):
        """Infer the volume position without rewriting the title."""
        series = "Starting on Hard Mode: God Levels, Got Problems"
        for suffix in (" Volume 1", ": Volume 1"):
            with self.subTest(suffix=suffix):
                row = {"title": series + suffix, "authors": ["Hanmenkyoushi"]}
                metadata = self.plugin._to_metadata(row, 0)
                self.assertEqual(metadata.title, row["title"])
                self.assertEqual(metadata.series, series)
                self.assertEqual(metadata.series_index, 1)
                row["series"] = [{"name": "Catalog Series", "position": 2}]
                metadata = self.plugin._to_metadata(row, 0)
                self.assertEqual(metadata.series, "Catalog Series")
                self.assertEqual(metadata.series_index, 2)

    def test_twilight_court_isbn_recovers_series_and_subjects(self):
        """Enrich the reported work from the matching Goodreads ISBN."""
        title = "Fairy Rings and Dragon Kings"
        authors = ["Amy Sumida"]
        isbn = "9781977552136"
        self.plugin.prefs["plugin_ids"] = "open_library,goodreads"

        def lookup(provider, query, people, identifiers, timeout):
            common = {"authors": authors, "isbn": isbn, "source": provider}
            if provider == "open_library":
                return [
                    dict(common, title=title, confidence=0.99, tags=["books", "work"])
                ]
            if identifiers.get("isbn") == isbn:
                return [
                    dict(
                        common,
                        title=title + " (The Twilight Court, #7)",
                        confidence=0.95,
                        genres=["Romance", "Fantasy", "Dragons"],
                        series=[{"name": "The Twilight Court", "position": "7"}],
                    )
                ]
            return []

        with patch.object(self.plugin, "_search", side_effect=lookup) as search:
            matches = self.identify(
                title=title + " (The Twilight Court)", authors=authors
            )
        self.assertTrue(
            any(
                call.args[0] == "goodreads" and call.args[3].get("isbn") == isbn
                for call in search.call_args_list
            )
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].title, title)
        self.assertEqual(matches[0].series, "The Twilight Court")
        self.assertEqual(matches[0].series_index, 7)
        self.assertEqual(matches[0].tags, ["Romance", "Fantasy", "Dragons"])

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

    def test_google_books_is_checked_with_or_without_a_provider_list(self):
        """Send one Google Books lookup through the engine even if omitted."""
        isbn = "0711217564"
        for configured in ("", "fixture", "fixture,google_books,google_books"):
            with self.subTest(configured=configured):
                self.plugin.prefs["plugin_ids"] = configured
                self.requests.clear()
                self.identify(title="The Spice Routes", identifiers={"isbn": isbn})
                searches = [
                    query
                    for path, query in self.requests
                    if path == "/plugins/google_books/search"
                ]
                self.assertEqual(len(searches), 1)
                self.assertEqual(searches[0]["isbn"], [isbn])

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
        self.assertFalse(any(item.title == "My Curvy Belle" for item in results))

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
        self.assertEqual(len(searches), 3)
        self.assertEqual(searches[1]["query"], ["A Specter and an Invasion"])
        self.assertNotIn("author", searches[1])
        self.assertEqual(searches[2]["query"], ["goodreads:55033599"])
        self.assertNotIn("author", searches[2])

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
            if provider == "hardcover" and authors == ["B Writer"]:
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
            [
                call.args[2]
                for call in search.call_args_list
                if call.args[0] == "hardcover"
            ],
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

    def test_cover_choices_include_larger_matching_images(self):
        """Rank matching cover sizes and reject wrong-title or wrong-edition images."""
        isbn = "9780547928227"
        rows = [
            {
                "title": "The Hobbit",
                "authors": ["J. R. R. Tolkien"],
                "isbn": isbn,
                "source": "one",
                "cover_url": "https://example.test/small.png",
                "confidence": 0.99,
            },
            {
                "title": "The Hobbit",
                "authors": ["J. R. R. Tolkien"],
                "isbn": isbn,
                "source": "two",
                "cover_url": "https://example.test/large.png",
                "confidence": 0.8,
            },
            {
                "title": "Different Book",
                "authors": ["J. R. R. Tolkien"],
                "isbn": isbn,
                "source": "bad",
                "cover_url": "https://example.test/wrong.png",
            },
        ]
        images = {
            "https://example.test/small.png": png(265, 400),
            "https://example.test/large.png": png(1200, 1800),
        }
        queue = Queue()
        with (
            patch.object(
                self.plugin, "_configured_provider_ids", return_value=["test"]
            ),
            patch.object(self.plugin, "_search", return_value=rows),
            patch.object(
                self.plugin, "_http_bytes", side_effect=lambda url, timeout: images[url]
            ),
        ):
            self.plugin.download_cover(
                self.log,
                queue,
                self.abort,
                title="The Hobbit",
                authors=["J. R. R. Tolkien"],
                identifiers={"isbn": isbn},
            )
        self.assertEqual(
            [item[1] for item in list(queue.queue)],
            [
                images["https://example.test/large.png"],
                images["https://example.test/small.png"],
            ],
        )

    def test_sparse_goodreads_search_recovers_cover_by_verified_id(self):
        """A sparse title search must recover its matching Goodreads cover."""
        sparse = {
            "title": "Prettiest Psycho",
            "authors": ["Crystal North"],
            "source": "goodreads",
            "source_id": "191691219",
            "series": [],
            "cover_url": None,
        }
        rich = dict(
            sparse,
            title="Prettiest Psycho (The Asylum, #1)",
            series=[{"name": "The Asylum", "position": "1"}],
            cover_url="https://example.test/psycho.jpg",
            extra={"goodreads_id": "191691219"},
        )
        image = png(900, 1350)

        def search(provider, query, *args, **kwargs):
            return [dict(rich if query.startswith("goodreads:") else sparse)]

        for identifiers in (
            {},
            {"goodreads": "191691219"},
            {"metadata_engine_goodreads": "191691219"},
        ):
            self.plugin._cover_urls.clear()
            queue = Queue()
            with (
                patch.object(
                    self.plugin, "_configured_provider_ids", return_value=["goodreads"]
                ),
                patch.object(self.plugin, "_search", side_effect=search),
                patch.object(self.plugin, "_http_bytes", return_value=image),
            ):
                self.plugin.download_cover(
                    self.log,
                    queue,
                    self.abort,
                    title="Prettiest Psycho",
                    authors=["Crystal North"],
                    identifiers=identifiers,
                )
            self.assertFalse(queue.empty(), repr(identifiers))
            self.assertEqual(queue.get_nowait()[1], image)
        self.plugin._cover_urls.clear()
        self.plugin._remember_cover(rich)
        self.assertEqual(
            self.plugin._cached_cover({"goodreads": "191691219"}), rich["cover_url"]
        )
        self.assertEqual(
            self.plugin._cached_cover({"metadata_engine_goodreads": "191691219"}),
            rich["cover_url"],
        )
        wrong = dict(rich, source_id="999", extra={"goodreads_id": "999"})
        with (
            patch.object(
                self.plugin, "_configured_provider_ids", return_value=["goodreads"]
            ),
            patch.object(
                self.plugin,
                "_search",
                side_effect=lambda provider, query, *a, **k: [wrong]
                if query.startswith("goodreads:")
                else [sparse],
            ),
        ):
            self.plugin._cover_urls.clear()
            queue = Queue()
            self.plugin.download_cover(
                self.log,
                queue,
                self.abort,
                title="Prettiest Psycho",
                authors=["Crystal North"],
                identifiers={},
            )
            self.assertTrue(queue.empty())

    def test_cover_recovery_is_bounded_and_does_not_follow_new_ids(self):
        """Visit each initial provider ID at most once and ignore derived IDs."""
        rows = [
            {
                "title": "Example",
                "authors": ["A Writer"],
                "source": "goodreads",
                "source_id": str(number),
                "cover_url": None,
            }
            for number in (1, 1, 2, 3, 4, 5)
        ]

        def search(provider, query, *args, **kwargs):
            value = query.split(":")[-1]
            return [
                {
                    "title": "Example",
                    "authors": ["A Writer"],
                    "source": "goodreads",
                    "source_id": value,
                    "cover_url": "https://example.test/" + value,
                    "extra": {"goodreads_id": value, "new_id": "999"},
                }
            ]

        with patch.object(self.plugin, "_search", side_effect=search) as mocked:
            found = self.plugin._recover_cover_records(
                self.log,
                self.abort,
                ["goodreads"],
                rows,
                "Example",
                ["A Writer"],
                {},
                5,
            )
        self.assertEqual(len(found), 4)
        self.assertEqual(mocked.call_count, 4)
        queries = [call.args[1] for call in mocked.call_args_list]
        self.assertEqual(
            queries, ["goodreads:1", "goodreads:2", "goodreads:3", "goodreads:4"]
        )
        self.assertNotIn("goodreads:999", queries)

    def test_structured_audio_series_merges_complete_work_metadata(self):
        """Combine shortened titles only with corroborated series and contributors."""
        apple = {
            "source": "itunes",
            "source_id": "123",
            "title": "F*ck Off",
            "authors": ["Crystal North", "Jaye Pratt"],
            "series": [{"name": "Knox Academy", "position": 2}],
            "genres": ["Romance"],
            "cover_url": "https://example.test/large.jpg",
            "confidence": 0.99,
        }
        goodreads = {
            "source": "goodreads",
            "source_id": "456",
            "title": "F*ck Off: Term Two (Knox Academy Trilogy #2)",
            "authors": ["Crystal North", "Jaye Pratt", "Jaye Cox"],
            "description": "A complete synopsis.",
            "genres": ["Romance", "Dark"],
            "confidence": 0.9,
        }
        target = {
            "title": "F*ck Off: Knox Academy - Term 2",
            "authors": ["Crystal North", "Jaye Pratt"],
        }
        for left, right in ((target, apple), (apple, goodreads), (goodreads, apple)):
            self.assertTrue(self.plugin._work_description_matches(left, right))
        for rows in ([apple, goodreads], [goodreads, apple]):
            merged = self.plugin._dedupe(rows)
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["description"], "A complete synopsis.")
            self.assertEqual(merged[0]["series"][0]["position"], 2)
            self.assertEqual(set(merged[0]["genres"]), {"Romance", "Dark"})
            self.assertEqual(merged[0]["cover_url"], apple["cover_url"])
        for wrong in (
            dict(apple, series=[]),
            dict(apple, series=[{"name": "Knox Academy"}]),
            dict(apple, series=[{"name": "Knox Academy", "position": 1}]),
            dict(apple, series=[{"name": "Other Academy", "position": 2}]),
            dict(apple, authors=["Unrelated Writer"]),
        ):
            with self.subTest(wrong=wrong):
                self.assertFalse(self.plugin._work_description_matches(target, wrong))
                self.assertEqual(len(self.plugin._dedupe([goodreads, wrong])), 2)
        contradictory = dict(
            apple,
            title="F*ck Off: Knox Academy, Book 1 (Unabridged)",
        )
        self.assertFalse(self.plugin._work_description_matches(target, contradictory))

    def test_cover_matches_verified_spelled_series_number(self):
        """Match the reported book without accepting a neighboring series entry."""
        target = {
            "title": "F*ck Off: Knox Academy - Term 2",
            "authors": ["Crystal North", "Jaye Pratt"],
        }
        row = {
            "title": "F*ck Off: Term Two (Knox Academy Trilogy #2)",
            "authors": ["Crystal North", "Jaye Pratt", "Jaye Cox"],
            "cover_url": "https://example.test/cover.jpg",
        }
        self.assertTrue(self.plugin._work_description_matches(target, row))
        self.assertFalse(
            self.plugin._work_description_matches(
                target, dict(row, title="F*ck Off: Term One (Knox Academy Trilogy #1)")
            )
        )
        self.assertFalse(
            self.plugin._work_description_matches(
                target, dict(row, title="F*ck Off: Term Two (Other Academy #2)")
            )
        )
        apple = {
            "title": "F*ck Off: Knox Academy, Book 2 (Unabridged)",
            "authors": ["Crystal North", "Jaye Pratt"],
            "cover_url": "https://example.test/cover.jpg",
        }
        self.assertTrue(self.plugin._work_description_matches(target, apple))
        self.assertFalse(
            self.plugin._work_description_matches(
                target, dict(apple, title="F*ck Off: Knox Academy, Book 1 (Unabridged)")
            )
        )
        self.assertFalse(
            self.plugin._work_description_matches(
                target,
                dict(apple, title="F*ck Off: Other Academy, Book 2 (Unabridged)"),
            )
        )
        image = png(800, 1200)
        queue = Queue()
        with (
            patch.object(
                self.plugin, "_configured_provider_ids", return_value=["goodreads"]
            ),
            patch.object(self.plugin, "_search", return_value=[row]),
            patch.object(self.plugin, "_http_bytes", return_value=image),
        ):
            self.plugin.download_cover(
                self.log, queue, self.abort, **target, identifiers={}
            )
        self.assertFalse(queue.empty())
        self.assertEqual(queue.get_nowait()[1], image)

    def test_cover_matches_title_with_verified_series_inserted(self):
        """A series name inserted into the title must not hide a matching cover."""
        image = png(800, 1200)
        row = {
            "title": "F*ck You: Term One (Knox Academy Trilogy #1)",
            "authors": ["Crystal North", "Jaye Cox"],
            "source": "goodreads",
            "source_id": "53133034",
            "series": [],
            "cover_url": "https://example.test/cover.jpg",
        }
        target = {
            "title": "F*ck You: Knox Academy - Term One",
            "authors": ["Jaye Cox", "Crystal North"],
        }
        self.assertTrue(self.plugin._work_description_matches(target, row))
        for wrong in (
            dict(row, title="F*ck You: Term Two (Knox Academy Trilogy #2)"),
            dict(row, title="F*ck You: Term One (Another Academy Trilogy #1)"),
            dict(row, authors=["Other Writer"]),
        ):
            self.assertFalse(self.plugin._work_description_matches(target, wrong))
        queue = Queue()

        def search(provider, query, authors, *args, **kwargs):
            return [] if authors and len(authors) > 1 else [row]

        with (
            patch.object(
                self.plugin, "_configured_provider_ids", return_value=["goodreads"]
            ),
            patch.object(self.plugin, "_search", side_effect=search) as searches,
            patch.object(self.plugin, "_http_bytes", return_value=image),
        ):
            self.plugin.download_cover(
                self.log, queue, self.abort, **target, identifiers={}
            )
        self.assertTrue(any(call.args[2] is None for call in searches.call_args_list))
        self.assertFalse(queue.empty())
        self.assertEqual(queue.get_nowait()[1], image)

    def test_trigger_warnings_merge_only_from_matching_book(self):
        """Show verified warning labels once without copying unrelated warnings."""
        book = {
            "title": "Example",
            "authors": ["A Writer"],
            "source": "books",
            "description": "Book synopsis.",
            "confidence": 1,
        }
        warning = {
            "title": "Example",
            "authors": ["A Writer"],
            "source": "storygraph",
            "trigger_warnings": ["Violence", "Abuse"],
            "parental_warnings": ["Sexual content"],
        }
        wrong = {
            "title": "Example",
            "authors": ["Other Writer"],
            "source": "other",
            "trigger_warnings": ["Wrong book warning"],
        }
        merged = self.plugin._dedupe([book, warning, wrong])
        metadata = self.plugin._to_metadata(merged[0], 0)
        self.assertIn("Book synopsis.", metadata.comments)
        self.assertTrue(
            metadata.comments.startswith("trigger warnings: Violence, Abuse\n\n")
        )
        self.assertIn("Parental warnings: Sexual content", metadata.comments)
        self.assertNotIn("Wrong book warning", metadata.comments)
        existing = dict(
            book,
            description="Trigger warnings: Violence\n\nBook synopsis.",
            trigger_warnings=["Violence"],
        )
        self.assertEqual(
            self.plugin._to_metadata(existing, 0).comments.count("Violence"), 1
        )

    def test_trigger_warning_header_formats_html_and_omits_empty_labels(self):
        """Keep comments intact and use a separate paragraph for HTML warnings."""
        row = {"title": "Example", "description": "<p>Synopsis.</p>"}
        self.assertEqual(self.plugin._to_metadata(row, 0).comments, row["description"])
        row["trigger_warnings"] = ["Violence & Abuse", "violence & abuse"]
        self.assertEqual(
            self.plugin._to_metadata(row, 0).comments,
            "<p>trigger warnings: Violence &amp; Abuse</p><p>Synopsis.</p>",
        )

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
    expected_tests = suite.countTestCases()
    if expected_tests < 57:
        raise RuntimeError("The real-Calibre regression suite was not fully collected")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun != expected_tests or result.skipped:
        raise RuntimeError("The real-Calibre regression suite did not run every test")
    sys.exit(0 if result.wasSuccessful() else 1)
