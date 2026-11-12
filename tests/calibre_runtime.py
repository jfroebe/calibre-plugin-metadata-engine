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
from copy import deepcopy
from itertools import permutations
from pathlib import Path
from queue import Queue
from threading import Barrier, Event
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
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
        # Keep the new direct ISBN redirect off the network in fixture tests.
        if hasattr(self.plugin, "_http_response"):
            real_response = self.plugin._http_response

            def fixture_response(url, timeout):
                if url.startswith("https://www.goodreads.com/book/isbn/"):
                    return b"", url
                return real_response(url, timeout)

            transport = patch.object(
                self.plugin, "_http_response", side_effect=fixture_response
            )
            transport.start()
            self.addCleanup(transport.stop)
        self.log = logging.getLogger("calibre-validation")
        self.abort = Event()

    def identify(self, **kwargs):
        """Invoke Calibre's metadata source interface and drain its results."""
        queue = Queue()
        error = self.plugin.identify(self.log, queue, self.abort, **kwargs)
        self.assertIsNone(error)
        return list(queue.queue)

    def test_edition_decorated_donors_supply_full_synopsis_and_coauthors(self):
        """Validate the final book through real request parsing and merge paths."""
        title = "Smoke & Spice"
        full_title = "Smoke & Spice : Cooking With Smoke, the Real Way to Barbecue"
        isbn = "9781558328365"
        synopsis = (
            "This expanded cookbook explains how to cook slowly with smoke rather than "
            "direct heat. It guides home cooks through selecting equipment, controlling "
            "the fire, and preparing regional barbecue dishes. Detailed recipes cover "
            "meat, fish, vegetables, sauces, side dishes, and desserts. Practical advice "
            "helps readers understand the different results produced by charcoal grills, "
            "water smokers, and wood-burning pits. The illustrated third edition includes "
            "450 recipes and step-by-step guidance for planning a complete outdoor meal."
        )
        initial = {
            "internet_archive": {
                "title": full_title,
                "authors": ["Cheryl Alters Jamison"],
                "isbn": isbn,
                "confidence": 0.99,
                "description": None,
                "genres": ["Barbecuing", "Smoked foods"],
            },
            "google_books": {
                "title": "Smoke & Spice, Updated and Expanded 3rd Edition",
                "authors": ["Cheryl Jamison", "Bill Jamison"],
                "isbn": isbn,
                "confidence": 0.95,
                "description": "A practical guide to smoking food outdoors.",
                "genres": ["Cooking"],
            },
            "goodreads": {
                "title": "Smoke & Spice, Updated and Expanded 3rd Edition: Cooking With Smoke, the Real Way to Barbecue",
                "authors": ["Cheryl Alters Jamison", "Bill Jamison"],
                "isbn": isbn,
                "confidence": 0.90,
                "description": synopsis,
                "genres": ["Cookbooks", "Food"],
            },
            "open_library": {
                "title": "Smoke & Spice, Revised 2nd Edition",
                "authors": ["Cheryl Alters Jamison", "Bill Jamison"],
                "isbn": "9781558322615",
                "confidence": 1.0,
                "description": "OLDER EDITION CONTENT must not supply this synopsis.",
            },
        }

        class CalibreLog:
            """Record arguments as Calibre does, without printf interpolation."""

            def __init__(self):
                self.lines = []

            def info(self, *parts):
                self.lines.append(parts)

            warning = info
            error = info
            exception = info

        self.log = CalibreLog()
        self.plugin.prefs["plugin_ids"] = ",".join(initial)

        def api(url, timeout):
            provider = urlsplit(url).path.split("/")[-2]
            return (
                {"results": [dict(initial[provider])]}
                if provider in initial
                else {"results": []}
            )

        with patch.object(self.plugin, "_http_json", side_effect=api):
            results = self.identify(
                title=title,
                authors=["Cheryl Alters Jamison"],
                identifiers={"isbn": isbn},
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].comments, synopsis)
        self.assertEqual(results[0].authors, ["Cheryl Alters Jamison", "Bill Jamison"])
        self.assertIn("Cookbooks", results[0].tags)
        self.assertIn("Barbecuing", results[0].tags)
        self.assertEqual(results[0].get_identifiers()["isbn"], isbn)
        self.assertTrue(all(len(parts) == 1 for parts in self.log.lines))
        self.assertFalse(
            any("%d" in str(parts) or "%s" in str(parts) for parts in self.log.lines)
        )

    def test_edition_title_normalization_requires_shared_valid_isbn(self):
        """Keep differing editions, subtitles and identities distinct."""
        isbn = "9781558328365"
        base = {
            "title": "Smoke & Spice",
            "authors": ["Cheryl Alters Jamison"],
            "isbn": isbn,
        }
        third = dict(base, title="Smoke and Spice, Updated and Expanded Third Edition")
        second = dict(base, title="Smoke & Spice (2nd Edition)")
        self.assertTrue(self.plugin._identity_consistent(base, third))
        self.assertFalse(self.plugin._identity_consistent(second, third))
        self.assertFalse(
            self.plugin._identity_consistent(
                dict(base, title="Smoke & Spice: Cooking With Smoke"),
                dict(third, title="Smoke & Spice, 3rd Edition: Cooking With Gas"),
            )
        )
        self.assertFalse(
            self.plugin._identity_consistent(
                base,
                dict(third, title="An Unrelated Book, 3rd Edition"),
            )
        )
        self.assertFalse(
            self.plugin._work_titles_compatible(
                {"title": base["title"]},
                {"title": third["title"]},
            )
        )
        self.assertFalse(
            self.plugin._compatible_identifiers(
                base,
                dict(third, isbn="9781558322615"),
            )
        )

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
            results = self.identify(
                title=title, authors=["Ian Rupert"], identifiers={"isbn": "0732911516"}
            )
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
        for value in (
            "1 volume",
            "2 volumes",
            "1 v.",
            "one volume",
            "1 volume (various pagings)",
            "227 p. :",
            "<p>&nbsp;</p>",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.plugin._usable_description(value))
        for value in (
            "A journey through spice markets.",
            "One volume changes her life.",
        ):
            self.assertEqual(self.plugin._usable_description(value), value)

    def test_transport_timeout_retries_once_and_stops(self):
        """Bound timeout recovery at the transport boundary used by all requests."""
        with patch.object(
            importlib.import_module(self.plugin.__class__.__module__),
            "urlopen",
            side_effect=URLError(TimeoutError("timed out")),
        ) as opening:
            with self.assertRaises(RuntimeError):
                self.plugin._http_bytes(self.url + "/timeout", 2)
        self.assertEqual(opening.call_count, 2)

    def test_transport_timeout_recovers_without_losing_payload(self):
        """Preserve a successful API response after one transient timeout."""
        response = urllib.request.urlopen(self.url + "/plugins", timeout=2)
        self.addCleanup(response.close)
        with patch.object(
            importlib.import_module(self.plugin.__class__.__module__),
            "urlopen",
            side_effect=[URLError(TimeoutError("timed out")), response],
        ) as opening:
            payload = self.plugin._http_json(self.url + "/plugins", 2)
        self.assertIsInstance(payload, dict)
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

    def test_catalog_series_suffix_needs_corroboration_before_enrichment(self):
        """Use a verified series and genres without borrowing the paperback edition."""
        title, author, series = "Moon Deeds", "Palmer Pickering", "Star Children Saga"
        asin, isbn = "B07S9PDKTW", "9781732568822"
        synopsis = "Twins Cassidy and Torr search the galaxy for the Star Children."
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for joined, donor_series, donor_position in (
            (False, series, 1),
            (True, series, 1),
            (False, series, 2),
            (False, "Other Saga", 1),
        ):
            with self.subTest(
                joined=joined, series=donor_series, position=donor_position
            ):
                suffix = f"{series} : One"
                candidate = {
                    "source": "google_books",
                    "source_id": "bW4GyAEACAAJ",
                    "title": f"{title}: {suffix}" if joined else title,
                    "subtitle": None if joined else suffix,
                    "authors": [author],
                    "isbn": isbn,
                    "description": synopsis,
                }
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": 44323726,
                        "title": title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "bookGenres": [{"genre": {"name": "Space Opera"}}],
                        "bookSeries": [
                            {
                                "series": {"__ref": "Series:1"},
                                "userPosition": str(donor_position),
                            }
                        ],
                        "details": {"isbn13": "9781732568808", "asin": "1732568804"},
                        "imageUrl": "https://example.test/paperback.jpg",
                    },
                    "Author:1": {"name": author},
                    "Series:1": {"title": donor_series},
                }
                payload = json.dumps({"props": {"pageProps": {"apolloState": state}}})

                def search(provider, query, *args, **kwargs):
                    return (
                        [deepcopy(candidate)]
                        if provider == "google_books" and query == title
                        else []
                    )

                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            b"<span book_id='44323726'></span>",
                            f'<script id="__NEXT_DATA__">{payload}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    (result,) = self.identify(
                        title=title, authors=[author], identifiers={"mobi-asin": asin}
                    )
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(result.get_identifiers()["isbn"], isbn)
                self.assertEqual(result.get_identifiers()["asin"], asin)
                self.assertEqual(result.comments, synopsis)
                self.assertIsNone(self.plugin._cached_cover(result.get_identifiers()))
                if (donor_series, donor_position) == (series, 1):
                    self.assertEqual(result.title, title)
                    self.assertEqual(
                        (result.series, result.series_index), (series, 1.0)
                    )
                    self.assertEqual(result.tags, ["Space Opera"])
                else:
                    self.assertIn(series, result.title)
                    self.assertFalse(result.series)
                    self.assertFalse(result.tags)

    def test_authorless_homonym_does_not_block_verified_kindle_fallback(self):
        """Recover the requested Kindle edition or reject the unverified homonym."""
        title = "The Devil's Apprentice"
        author = "Patrick Stewart"
        asin = "B081S9TTSF"
        goodreads_id = "48908672"
        synopsis = "Alex sees angels and demons walking around the streets of London."
        self.plugin.prefs["plugin_ids"] = "open_library,goodreads"
        homonym = {
            "source": "open_library",
            "source_id": "OL46163002W",
            "title": title,
            "authors": [],
            "isbn": "9788763861595",
            "published_year": 2018,
        }

        def search(provider, *args, **kwargs):
            return [deepcopy(homonym)] if provider == "open_library" else []

        for credit, edition_asin in (
            ("Patrick  Stewart", asin),
            ("Kenneth Bøgh Andersen", asin),
            ("Patrick  Stewart", "B000000001"),
        ):
            with self.subTest(credit=credit, asin=edition_asin):
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": int(goodreads_id),
                        "titleComplete": title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "details": {"asin": edition_asin},
                        "description": synopsis,
                        "bookGenres": [{"genre": {"name": "Urban Fantasy"}}],
                    },
                    "Author:1": {"name": credit},
                }
                payload = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                suggestions = json.dumps([{"bookId": goodreads_id}]).encode()
                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            suggestions,
                            f'<script id="__NEXT_DATA__">{payload}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title, authors=[author], identifiers={"mobi-asin": asin}
                    )
                self.assertEqual(fetch.call_count, 2)
                if credit != "Patrick  Stewart" or edition_asin != asin:
                    self.assertFalse(results)
                    continue
                (result,) = results
                self.assertEqual(result.title, title)
                self.assertEqual(result.authors, [author])
                self.assertEqual(result.comments, synopsis)
                self.assertEqual(result.tags, ["Urban Fantasy"])
                ids = result.get_identifiers()
                self.assertEqual(ids.get("asin"), asin)
                self.assertEqual(ids.get("goodreads"), goodreads_id)
                self.assertFalse(ids.keys() & {"isbn", "openlibrary"})

    def test_genreless_kindle_match_recovers_verified_work_shelves(self):
        """Return useful shelf genres only for the matching Kindle work."""
        title, author, asin = "Supers", "Patrick Stewart", "B088T3V8TJ"
        goodreads_id, work_id = "53443828", "83162363"
        synopsis = "Lucas hunts the survivors who gained superpowers from meteorites."
        self.plugin.prefs["plugin_ids"] = "goodreads"
        shelves = b"""
        <a href="/genres/fantasy">Browse fantasy</a>
        <div class="shelfStat"><div><a href="/genres/to-read">to-read</a></div></div>
        <div class="shelfStat"><div><a href="/genres/superheros">superheros</a></div></div>
        <div class="shelfStat"><div><a href="/genres/superheroes">superheroes</a></div></div>
        <div class="shelfStat"><div><a href="/genres/urban-fantasy">urban-fantasy</a></div></div>
        <div class="shelfStat"><div><a href="/genres/audio-book">audio-book</a></div></div>
        <div class="shelfStat"><div><a href="/genres/my-favorites">my-favorites</a></div></div>
        """
        for edition_asin, initial_tags in (
            (asin, []),
            ("B000000001", []),
            (asin, ["Fantasy"]),
        ):
            with self.subTest(asin=edition_asin, initial_tags=initial_tags):
                candidate = {
                    "source": "goodreads",
                    "source_id": goodreads_id,
                    "title": title,
                    "authors": [author],
                    "asin": asin,
                    "published_year": 2020,
                    "description": synopsis,
                    "genres": initial_tags,
                }
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": int(goodreads_id),
                        "title": title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "details": {"asin": edition_asin},
                        "work": {"__ref": "Work:1"},
                    },
                    "Author:1": {"name": author},
                    "Work:1": {"legacyId": int(work_id)},
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[candidate]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                            shelves,
                        ],
                    ) as fetch,
                ):
                    (result,) = self.identify(
                        title=title, authors=[author], identifiers={"mobi-asin": asin}
                    )
                if initial_tags:
                    self.assertEqual(result.tags, ["Fantasy"])
                    fetch.assert_not_called()
                elif edition_asin != asin:
                    self.assertFalse(result.tags)
                    self.assertEqual(fetch.call_count, 1)
                else:
                    self.assertEqual(result.tags, ["Superheroes", "Urban Fantasy"])
                    self.assertEqual(fetch.call_count, 2)
                    self.assertTrue(
                        fetch.call_args.args[0].endswith(f"/work/shelves/{work_id}")
                    )
                self.assertEqual(result.authors, [author])
                self.assertEqual(result.comments, synopsis)
                self.assertEqual(result.pubdate.year, 2020)
                self.assertEqual(result.get_identifiers()["asin"], asin)

    def test_goodreads_kindle_search_recovers_title_without_marketing_subtitle(self):
        """Retry an empty decorated-title search and verify the complete identity."""
        title = "Branding Belle: A Reverse Harem Contemporary Standalone"
        authors = ["Crystal North", "Kira Roman"]
        asin = "B096HHSWWR"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for catalog_title, catalog_asin, coauthor, matches in (
            ("Branding Belle", asin, authors[1], True),
            ("Branding Belle", "B000000001", authors[1], False),
            ("Branding Belle", asin, "Another Writer", False),
            ("Branding Belle: A Different Subtitle", asin, authors[1], False),
            ("Branding Bella", asin, authors[1], False),
        ):
            with self.subTest(
                title=catalog_title, asin=catalog_asin, coauthor=coauthor
            ):
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": 58325014,
                        "titleComplete": catalog_title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "secondaryContributorEdges": [
                            {"role": "Author", "node": {"__ref": "Author:2"}}
                        ],
                        "details": {"asin": catalog_asin},
                        "description": "Belle travels to Miami for her dream tattoo.",
                        "bookGenres": [{"genre": {"name": "Contemporary Romance"}}],
                    },
                    "Author:1": {"name": authors[0]},
                    "Author:2": {"name": coauthor},
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            b"[]",
                            b'[{"bookId": "58325014", "title": "Branding Belle"}]',
                            f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title, authors=authors, identifiers={"mobi-asin": asin}
                    )
                if matches:
                    self.assertEqual(len(results), 1)
                    (result,) = results
                    self.assertEqual(result.title, "Branding Belle")
                    self.assertEqual(result.authors, authors)
                    self.assertEqual(result.get_identifiers()["asin"], asin)
                    self.assertEqual(result.get_identifiers()["goodreads"], "58325014")
                    self.assertEqual(result.tags, ["Contemporary Romance"])
                    self.assertIn("dream tattoo", result.comments)
                else:
                    self.assertFalse(results)
                self.assertEqual(fetch.call_count, 3)
                self.assertEqual(
                    parse_qs(urlsplit(fetch.call_args_list[1].args[0]).query)["q"],
                    [asin],
                )

    def test_goodreads_fallback_verifies_anthology_asin_and_credits(self):
        """Recover a verified box set with alternate credits and an included series."""
        title = "Spice Box: Sixteen Steamy Stories"
        authors = [
            "Raine Miller",
            "Cathryn Fox",
            "Gabrielle Bisset",
            "Erika Wilde",
            "Nina Lane",
        ]
        asin, isbn = "B00J3CEULQ", "9781311437068"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for identifier, credits in (
            (
                asin,
                [
                    "Raine Miller",
                    "Cathryn Fox",
                    "K.M. Scott",
                    "Erika Wilde",
                    "Nina Lane",
                ],
            ),
            ("B000000001", authors),
            (asin, ["Raine Miller", "Other Writer"]),
        ):
            with self.subTest(asin=identifier, authors=credits):
                edges = [
                    {"role": "Author", "node": {"__ref": f"Author:{index}"}}
                    for index in range(len(credits))
                ]
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": 22027809,
                        "title": title,
                        "primaryContributorEdge": edges[0],
                        "secondaryContributorEdges": edges[1:],
                        "details": {"asin": identifier, "isbn13": isbn},
                        "description": "A collection of sixteen stories.",
                        "bookGenres": [{"genre": {"name": "Anthologies"}}],
                        "bookSeries": [
                            {
                                "series": {"__ref": "Series:1"},
                                "userPosition": "1 included",
                            }
                        ],
                    },
                    "Series:1": {"title": "Spiral of Bliss"},
                    **{
                        f"Author:{index}": {"name": name}
                        for index, name in enumerate(credits)
                    },
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            b"<span book_id='22027809'></span>",
                            f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title.replace(":", ";"),
                        authors=authors,
                        identifiers={"mobi-asin": asin},
                    )
                self.assertEqual(fetch.call_count, 2)
                query = parse_qs(urlsplit(fetch.call_args_list[0].args[0]).query)
                self.assertEqual(query["q"], [f"{title} Raine Miller"])
                if "K.M. Scott" not in credits:
                    self.assertFalse(results)
                    continue
                (result,) = results
                self.assertEqual(result.title, title)
                self.assertEqual(result.authors, [*authors, "K. M. Scott"])
                self.assertEqual(result.get_identifiers()["asin"], asin)
                self.assertEqual(result.get_identifiers()["isbn"], isbn)
                self.assertEqual(result.tags, ["Anthologies"])
                self.assertFalse(result.series)

    def test_unavailable_saved_goodreads_id_uses_verified_bibliography(self):
        """Match the original short story when the saved edition page is gone."""
        title, author = "Henrikk's Punishment", "Darcy Fayton"
        asin, goodreads_id = "B0CVZJHQ6W", "208896222"
        ids = {"amazon": asin, "goodreads": goodreads_id, "mobi-asin": asin}
        self.plugin.prefs["plugin_ids"] = "goodreads"
        missing = b"<h1>Page not found</h1>"
        for response, listing_title, listing_author, matches in (
            (missing, title, author, True),
            (RuntimeError("HTTP 405: Human Verification"), title, author, True),
            (missing, "Henrikk's Prisoner", author, False),
            (missing, "Punished by the Vampire King", author, False),
            (missing, title, "Different Author", False),
        ):
            with self.subTest(title=listing_title, author=listing_author):
                bibliography = f"""
                    <h1 id="bnauthname">{listing_author}</h1>
                    <h3 class="series_title">List of Henrikk's Short Stories Books in Publication Order</h3>
                    <table id="books00121077">
                      <tr id="hide3886230">
                        <td>1</td>
                        <td><div class="titles"><a href="">{listing_title}</a></div></td>
                      </tr>
                    </table>
                """
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[response, bibliography.encode()],
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title, authors=[author], identifiers=ids
                    )
                self.assertEqual(
                    [call.args[0] for call in fetch.call_args_list],
                    [
                        f"https://www.goodreads.com/en/book/show/{goodreads_id}",
                        "https://www.booknotification.com/authors/darcy-fayton/",
                    ],
                )
                if matches:
                    self.assertEqual(len(results), 1)
                    (book,) = results
                    self.assertEqual(book.title, title)
                    self.assertEqual(book.authors, [author])
                    self.assertEqual(
                        (book.series, book.series_index), ("Henrikk's Short Stories", 1)
                    )
                    self.assertEqual(
                        book.get_identifiers(),
                        {"asin": asin, "metadata_engine_book_notification": "3886230"},
                    )
                    self.assertFalse(book.comments)
                    self.assertFalse(getattr(book, "has_cached_cover", False))
                else:
                    self.assertFalse(results)

    def test_saved_goodreads_record_requires_compatible_title_author_and_asin(self):
        """Resolve a live saved record directly and reject contradictory evidence."""
        title, author = "Henrikk's Punishment", "Darcy Fayton"
        asin, goodreads_id = "B0CVZJHQ6W", "208896222"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for returned_title, returned_author, returned_asin, matches in (
            (title, author, asin, True),
            (title, author, "B000000001", False),
            ("Henrikk's Prisoner", author, asin, False),
            (title, "Different Author", asin, False),
        ):
            with self.subTest(
                title=returned_title, author=returned_author, asin=returned_asin
            ):
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": int(goodreads_id),
                        "titleComplete": returned_title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "details": {"asin": returned_asin},
                        "description": "A short story from the vampire king's court.",
                        "bookGenres": [{"genre": {"name": "Vampires"}}],
                    },
                    "Author:1": {"name": returned_author},
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        return_value=f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title,
                        authors=[author],
                        identifiers={"goodreads": goodreads_id, "mobi-asin": asin},
                    )
                self.assertEqual(len(results), int(matches))
                if matches:
                    self.assertEqual(
                        results[0].get_identifiers()["goodreads"], goodreads_id
                    )
                    self.assertEqual(results[0].get_identifiers()["asin"], asin)
                self.assertEqual(fetch.call_count, 1)
                self.assertTrue(
                    fetch.call_args.args[0].endswith("/book/show/" + goodreads_id)
                )

    def test_goodreads_retired_edition_label_preserves_original_work_identity(self):
        """Recover the retired work without treating a rewrite as the same title."""
        title, author = "Bedded by the Headmaster", "Darcy Fayton"
        series = "Susie & Arken's Duet"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for catalog_title, catalog_author, matches in (
            (f"OLD RETIRED EDITION - {title} ({series}, #1)", author, True),
            (
                f"Retired edition — Not the 2027 Rewrite — {title} ({series}, #1)",
                author,
                True,
            ),
            (f"Old Retired Edition: {title} ({series}, #1)", "Other Author", False),
            ("Bedded by the Vampire Headmaster", author, False),
            ("OLD RETIRED EDITION - Shared by the Headmaster", author, False),
        ):
            with self.subTest(title=catalog_title, author=catalog_author):
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": 217994670,
                        "titleComplete": catalog_title,
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "details": {
                            "asin": "B0DHQPGWD7",
                            "publisher": "Independently Published",
                        },
                        "description": "Susie meets Arken at Volmasque Academy.",
                        "bookGenres": [{"genre": {"name": "Paranormal Romance"}}],
                        "bookSeries": [
                            {
                                "series": {"__ref": "Series:1"},
                                "userPosition": "1",
                            }
                        ],
                    },
                    "Author:1": {"name": catalog_author},
                    "Series:1": {"title": series},
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            b'[{"bookId": "217994670"}]',
                            f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                        ],
                    ),
                ):
                    results = self.identify(
                        title=title, authors=[author], identifiers={}
                    )
                if not matches:
                    self.assertFalse(results)
                    continue
                self.assertEqual(len(results), 1)
                (result,) = results
                self.assertEqual(result.title, title)
                self.assertEqual(result.authors, [author])
                self.assertEqual((result.series, result.series_index), (series, 1.0))
                self.assertEqual(result.get_identifiers()["goodreads"], "217994670")
                self.assertFalse(result.get_identifiers().keys() & {"asin", "isbn"})
                self.assertFalse(result.publisher)
                self.assertEqual(result.tags, ["Paranormal Romance"])
                self.assertIn("Volmasque Academy", result.comments)

    def test_goodreads_title_fallback_recovers_work_without_assigning_edition(self):
        """Match a year-decorated title and coauthors without assigning edition data."""
        title = "Every Layout Relearn CSS layout, by example (2019)"
        canonical = "Every Layout: Relearn CSS layout, by example"
        author = "Heydon Pickering"
        goodreads_id = "48932917"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for candidate_title, candidate_author in (
            (canonical, author),
            (canonical, "A Different Writer"),
            ("Every Layout: Relearn JavaScript layout, by example", author),
        ):
            with self.subTest(title=candidate_title, author=candidate_author):
                book = {
                    "__typename": "Book",
                    "legacyId": int(goodreads_id),
                    "title": candidate_title,
                    "description": "Learn to design reusable layouts with CSS.",
                    "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                    "secondaryContributorEdges": [
                        {"role": "Author", "node": {"__ref": "Author:2"}},
                        {"role": "Editor", "node": {"__ref": "Author:3"}},
                    ],
                    "bookGenres": [{"genre": {"name": "Programming"}}],
                    "imageUrl": "https://example.test/first-edition.jpg",
                    "details": {
                        "asin": "B0FSK9TC34",
                        "isbn13": "9780547951973",
                        "publisher": "Other Edition Press",
                    },
                }
                state = {
                    "Book:1": book,
                    "Author:1": {"name": candidate_author},
                    "Author:2": {"name": "Andy Bell"},
                    "Author:3": {"name": "Editor Name"},
                }
                payload = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            f"<span book_id='{goodreads_id}'></span>".encode(),
                            f'<script id="__NEXT_DATA__">{payload}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    results = self.identify(
                        title=title, authors=[author], identifiers={}
                    )
                self.assertEqual(fetch.call_count, 2)
                query = parse_qs(urlsplit(fetch.call_args_list[0].args[0]).query)
                self.assertEqual(
                    query["q"],
                    ["Every Layout Relearn CSS layout, by example Heydon Pickering"],
                )
                self.assertEqual(
                    fetch.call_args_list[1].args[0],
                    f"https://www.goodreads.com/en/book/show/{goodreads_id}",
                )
                if (candidate_title, candidate_author) != (canonical, author):
                    self.assertFalse(results)
                    continue
                (result,) = results
                self.assertEqual(result.title.casefold(), canonical.casefold())
                self.assertEqual(result.authors, [author, "Andy Bell"])
                self.assertEqual(result.tags, ["Programming"])
                self.assertEqual(result.comments, book["description"])
                self.assertEqual(
                    result.get_identifiers(),
                    {
                        "goodreads": goodreads_id,
                        "metadata_engine_goodreads": goodreads_id,
                    },
                )
                self.assertIsNone(result.publisher)
                self.assertIsNone(result.pubdate)
                self.assertIsNone(self.plugin._cached_cover(result.get_identifiers()))

    def test_goodreads_kindle_followup_recovers_only_verified_edition(self):
        """Recover synopsis, subjects and series from the exact Goodreads edition."""
        author = "Palmer Pickering"
        title = "Dark Town, A Fantasy LitRPG Adventure: Level One of the Dragon's Crawl"
        series = "Tales of Temerity"
        asin = "B0CYWJ48TS"
        isbn = "9781960530028"
        goodreads_id = "210280433"
        url = f"https://www.goodreads.com/en/book/show/{goodreads_id}"
        sparse = {
            "title": title,
            "authors": [author],
            "source": "goodreads",
            "source_id": goodreads_id,
            "extra": {"goodreads_id": goodreads_id, "goodreads_url": url},
        }
        book = {
            "__typename": "Book",
            "legacyId": int(goodreads_id),
            "titleComplete": f"{title} ({series} Book 1)",
            "description": "Temerity enters the underground Dragon's Game.",
            "details": {"asin": asin, "isbn13": isbn, "publisher": "Mythology Press"},
            "primaryContributorEdge": {"node": {"__ref": "Contributor:1"}},
            "bookGenres": [{"genre": {"name": "Fantasy"}}],
            "bookSeries": [{"userPosition": "1", "series": {"__ref": "Series:1"}}],
        }
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for returned_asin in (asin, "B000000001"):
            with self.subTest(returned_asin=returned_asin):
                state = {
                    "Book:1": dict(
                        book, details=dict(book["details"], asin=returned_asin)
                    ),
                    "Contributor:1": {"name": author},
                    "Series:1": {"title": series},
                }
                payload = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                html = f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'

                def search(provider, query, *args, **kwargs):
                    return (
                        [deepcopy(sparse)]
                        if provider == "goodreads" and query == title
                        else []
                    )

                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(
                        self.plugin, "_http_bytes", return_value=html.encode()
                    ) as fetch,
                ):
                    (result,) = self.identify(
                        title=f"{title} ({series} Book 1)",
                        authors=[author],
                        identifiers={"mobi-asin": asin},
                    )
                fetch.assert_called_once_with(url, 8)
                self.assertEqual(result.get_identifiers()["asin"], asin)
                if returned_asin == asin:
                    self.assertEqual(result.get_identifiers()["isbn"], isbn)
                    self.assertEqual(result.tags, ["Fantasy"])
                    self.assertEqual(result.comments, book["description"])
                    self.assertEqual(
                        (result.series, result.series_index), (series, 1.0)
                    )
                else:
                    self.assertNotIn("isbn", result.get_identifiers())
                    self.assertFalse(result.comments)
                    self.assertFalse(result.tags)

    def test_verified_work_restores_requested_numbered_series_suffix(self):
        """Restore series lost by a catalog that returns the matching bare title."""
        title = "Dark Town, A Fantasy LitRPG Adventure: Level One of the Dragon's Crawl"
        authors = ["Palmer Pickering"]
        identifiers = {"mobi-asin": "B0CYWJ48TS"}
        row = {
            "title": title,
            "authors": authors,
            "isbn": "9781960530028",
            "description": "Temerity enters the underground Dragon's Game.",
            "genres": ["Fantasy"],
        }
        requested = f"{title} (Tales of Temerity Book 1)"
        with patch.object(
            self.plugin,
            "_search",
            side_effect=lambda *args, **kwargs: [deepcopy(row)]
            if args[0] == "google_books"
            else [],
        ):
            (result,) = self.identify(
                title=requested, authors=authors, identifiers=identifiers
            )
        self.assertEqual((result.series, result.series_index), ("Tales of Temerity", 1))
        self.assertEqual(result.get_identifiers()["isbn"], row["isbn"])
        self.assertEqual(result.get_identifiers()["asin"], identifiers["mobi-asin"])
        for conflicting in (
            dict(row, asin="B000000001"),
            dict(row, authors=["Another Writer"]),
            dict(row, title=title + " Part Two"),
        ):
            self.assertEqual(
                self.plugin._with_requested_series(
                    conflicting, requested, authors, identifiers
                ),
                conflicting,
            )

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

        self.plugin.prefs["plugin_ids"] = "google_books,tertulia_subjects"
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

    def test_indexed_catalog_title_uses_verified_work_and_series(self):
        """Find the user's book without interpreting the middle index as a volume."""
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        title = "Star Wars - 201 - Shadows of the Empire"
        isbn = "9780553574135"
        authors = ["Steve Perry"]
        record = {
            "title": "Shadows of the Empire: Star Wars Legends",
            "authors": authors,
            "isbn": isbn,
            "genres": ["Science Fiction"],
            "description": "Prince Xizor conspires against Darth Vader.",
            "source": "google_books",
        }
        collection = {"name": "Star Wars Legends: Novels", "position": ""}
        goodreads = dict(record, title="Shadows of the Empire", series=[collection])
        self.plugin.prefs["plugin_ids"] = "google_books,goodreads"

        for candidate, expected in (
            (record, True),
            (dict(record, title="Shadows of the Empire", isbn="0553574132"), True),
            (dict(record, title="Star Wars — Shadows of the Empire"), True),
            (dict(record, title="Shadows of the Empire (Star Wars)"), True),
            (dict(record, title="Shadows of the Empire II"), False),
            (dict(record, title="Shadows of the Empire: Another Franchise"), False),
            (dict(record, authors=["Another Writer"]), False),
            (dict(record, isbn="9780593974766"), False),
        ):
            with self.subTest(
                title=candidate["title"],
                isbn=candidate["isbn"],
                authors=candidate["authors"],
            ):
                with (
                    patch.object(
                        self.plugin,
                        "_search",
                        side_effect=lambda provider, *a, **k: [deepcopy(candidate)]
                        if provider == "google_books"
                        else [],
                    ) as search,
                    patch.object(
                        self.plugin,
                        "_http_response",
                        return_value=(
                            b"",
                            "https://www.goodreads.com/book/show/205051218-shadows-of-the-empire",
                        ),
                    ),
                    patch.object(
                        self.plugin, "_goodreads_book", return_value=goodreads
                    ),
                ):
                    rows = self.identify(
                        title=title, authors=authors, identifiers={"isbn": isbn}
                    )
                self.assertEqual(
                    search.call_args_list[0].args[1:4],
                    (
                        "Shadows of the Empire",
                        authors,
                        {"isbn": isbn},
                    ),
                )
                if not expected:
                    self.assertEqual(rows, [])
                    continue
                (book,) = rows
                book.identify_plugin = self.plugin
                book.relevance_in_source = 0
                (result,) = merge_identify_results({self.plugin: rows}, ThreadSafeLog())
                self.assertEqual(result.authors, authors)
                self.assertEqual(result.title, "Shadows of the Empire")
                self.assertEqual(
                    self.plugin._canonical_isbn(result.get_identifiers()["isbn"]), isbn
                )
                self.assertEqual(result.series, collection["name"])
                self.assertIsNone(result.series_index)
                self.assertEqual(result.tags, record["genres"])
                self.assertEqual(result.comments, record["description"])

    def test_indexed_title_without_isbn_requires_franchise_evidence(self):
        """Use the catalog franchise, author, and work title for ID-free matches."""
        title = "Archive – 017 – A New Dawn"
        authors = ["A Writer"]
        book = {"title": "A New Dawn", "authors": authors, "source": "google_books"}
        self.plugin.prefs["plugin_ids"] = "google_books"
        for candidate, expected in (
            (book, []),
            (
                dict(book, title="A New Dawn: Archive Legends"),
                ["A New Dawn: Archive Legends"],
            ),
            (dict(book, title=title), [title]),
        ):
            with self.subTest(title=candidate["title"]):

                def search(provider, query, *args, **kwargs):
                    if candidate["title"] == title and query != title:
                        return []
                    return [deepcopy(candidate)]

                with patch.object(self.plugin, "_search", side_effect=search):
                    rows = self.identify(title=title, authors=authors)
                self.assertEqual(
                    [row.title.casefold() for row in rows],
                    [value.casefold() for value in expected],
                )

    def test_verified_isbn_recovers_numbered_goodreads_series_after_merge(self):
        """Use the paperback's numbered series and reject other edition records."""
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        title = "Star Wars: Knight Errant"
        author = "John Jackson Miller"
        isbn = "9780345522641"
        row = {
            "title": title,
            "authors": [author],
            "isbn": isbn,
            "source": "hardcover",
            "description": "Jedi Kerra Holt faces the Dark Lords.",
            "genres": ["Science Fiction"],
            "publisher": "National Geographic Books",
        }
        series = [
            {"userPosition": "1.5", "series": {"__ref": "Series:1"}},
            {"userPosition": "", "series": {"__ref": "Series:2"}},
        ]
        self.plugin.prefs["plugin_ids"] = "hardcover,goodreads"
        isbn_url = f"https://www.goodreads.com/book/isbn/{isbn}"
        book_url = "https://www.goodreads.com/en/book/show/8323122"
        redirect = "https://www.goodreads.com/book/show/8323122-knight-errant"
        cases = (
            (isbn, author, "Knight Errant (Star Wars)", redirect, True),
            ("9781595826428", author, "Knight Errant (Star Wars)", redirect, False),
            (isbn, "Another Writer", "Knight Errant (Star Wars)", redirect, False),
            (isbn, author, "Knight Errant, Vol. 1: Aflame", redirect, False),
            (isbn, author, "Knight Errant (Star Wars)", isbn_url, False),
        )
        for catalog_isbn, person, catalog_title, location, match in cases:
            with self.subTest(
                isbn=catalog_isbn, author=person, title=catalog_title, location=location
            ):
                state = {
                    "Book:1": {
                        "__typename": "Book",
                        "legacyId": 8323122,
                        "titleComplete": catalog_title,
                        "details": {"isbn13": catalog_isbn, "publisher": "Del Rey"},
                        "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                        "bookSeries": series,
                    },
                    "Author:1": {"name": person},
                    "Series:1": {"title": "Star Wars: Knight Errant"},
                    "Series:2": {"title": "Star Wars Legends: Novels"},
                }
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                html = f'<script id="__NEXT_DATA__">{data}</script>'.encode()
                with (
                    patch.object(
                        self.plugin,
                        "_search",
                        side_effect=lambda provider, *a, **k: [deepcopy(row)]
                        if provider == "hardcover"
                        else [],
                    ),
                    patch.object(
                        self.plugin, "_http_response", return_value=(b"", location)
                    ) as lookup,
                    patch.object(
                        self.plugin, "_http_bytes", return_value=html
                    ) as detail,
                ):
                    (book,) = self.identify(
                        title=title, authors=[author], identifiers={"isbn": isbn}
                    )
                book.identify_plugin = self.plugin
                book.relevance_in_source = 0
                (result,) = merge_identify_results(
                    {self.plugin: [book]}, ThreadSafeLog()
                )
                expected = (title, 1.5) if match else (None, None)
                self.assertEqual((result.series, result.series_index), expected)
                self.assertEqual(result.get_identifiers()["isbn"], isbn)
                self.assertEqual(result.comments, row["description"])
                self.assertEqual(result.tags, row["genres"])
                self.assertEqual(result.publisher, row["publisher"])
                self.assertEqual(lookup.call_args.args[0], isbn_url)
                if location == isbn_url:
                    detail.assert_not_called()
                else:
                    self.assertEqual(detail.call_args.args[0], book_url)

    def test_isbn_genres_survive_catalog_timeouts_and_calibre_merge(self):
        """Use exact ISBN classifications when normal providers leave tags sparse."""
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        title = "Star Wars: Knight Errant"
        author = "John Jackson Miller"
        isbn = "9780345522641"
        synopsis = "Jedi Kerra Holt confronts the Dark Lords."
        hardcover = {
            "title": title,
            "authors": [author],
            "isbn": isbn,
            "description": synopsis,
            "publisher": "LucasBooks",
        }
        comic = {
            "title": "Star Wars: Knight Errant, Vol. 1: Aflame",
            "authors": [author],
            "isbn": "9781595826428",
            "tags": ["Graphic Novels"],
            "confidence": 0.99,
        }
        catalog = {
            "title": "Knight Errant",
            "subtitle": "Star Wars",
            "authors": [{"name": author}],
            "identifiers": {"isbn_10": ["0345522648"], "isbn_13": [isbn]},
            "subjects": [
                {"name": "Comics & graphic novels, science fiction"},
                {"name": "Science fiction comic books, strips"},
                {"name": "Fiction, science fiction, general"},
                {"name": "collectionID:swTOR"},
            ],
        }
        self.plugin.prefs["plugin_ids"] = "hardcover,open_library,goodreads"

        for tags, changes, expected, called in (
            ([], {}, {"Science Fiction"}, True),
            (["Fiction"], {}, {"Fiction", "Science Fiction"}, True),
            (["Space Opera"], {}, {"Space Opera"}, False),
            ([], {"identifiers": {"isbn_10": ["0345528298"]}}, set(), True),
            ([], {"title": comic["title"], "subtitle": None}, set(), True),
            ([], {"authors": [{"name": "Another Writer"}]}, set(), True),
        ):
            with self.subTest(tags=tags, changes=changes):

                def search(provider, *args, **kwargs):
                    if provider == "hardcover":
                        return [dict(hardcover, tags=tags)]
                    if provider == "google_books":
                        return [deepcopy(comic)]
                    raise TimeoutError("Catalog timed out")

                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(
                        self.plugin,
                        "_http_json",
                        return_value={f"ISBN:{isbn}": dict(catalog, **changes)},
                    ) as lookup,
                ):
                    rows = self.identify(
                        title=title, authors=[author], identifiers={"isbn": isbn}
                    )
                for rank, row in enumerate(rows):
                    row.identify_plugin = self.plugin
                    row.relevance_in_source = rank
                (result,) = merge_identify_results({self.plugin: rows}, ThreadSafeLog())
                self.assertEqual(set(result.tags), expected)
                self.assertEqual(result.get_identifiers()["isbn"], isbn)
                self.assertEqual(result.title, title)
                self.assertEqual(result.authors, [author])
                self.assertEqual(result.comments, synopsis)
                self.assertEqual(result.publisher, "LucasBooks")
                self.assertEqual(lookup.call_count, int(called))
                if called:
                    url, timeout = lookup.call_args.args
                    self.assertEqual(urlsplit(url).netloc, "openlibrary.org")
                    self.assertEqual(
                        parse_qs(urlsplit(url).query)["bibkeys"], [f"ISBN:{isbn}"]
                    )
                    self.assertLessEqual(timeout, 5)

    def test_failed_edition_lookup_recovers_verified_print_metadata(self):
        """Retry print catalogs and preserve the requested edition through Calibre."""
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        title = "Star Wars: Knight Errant"
        author = "John Jackson Miller"
        isbn = "9780307796530"
        paperback = {
            "title": title,
            "authors": [author],
            "isbn": "9780345522641",
            "description": "Jedi Kerra Holt faces the Dark Lords.",
            "genres": ["Science Fiction"],
            "source": "goodreads",
            "source_id": "8323122",
            "cover_url": "https://example.invalid/paperback.jpg",
            "publisher": "Del Rey",
            "published_year": 2011,
            "extra": {"format": "Mass Market Paperback", "goodreads_id": "8323122"},
        }
        self.plugin.prefs["plugin_ids"] = "goodreads,open_library,hardcover"
        scenarios = (
            ("paperback", paperback, True),
            (
                "franchise suffix",
                dict(paperback, title="Knight Errant (Star Wars)"),
                True,
            ),
            (
                "hardcover",
                dict(paperback, extra={"physical_format": "Hardcover"}),
                True,
            ),
            ("audiobook", dict(paperback, metadata_type="audiobook"), False),
            ("comic", dict(paperback, metadata_type="graphic novel"), False),
            ("wrong work", dict(paperback, title=title + ", Vol. 1: Aflame"), False),
            ("wrong author", dict(paperback, authors=["Another Author"]), False),
            ("unknown binding", dict(paperback, extra={}), False),
        )
        for name, candidate, matches in scenarios:
            with self.subTest(name=name):

                def search(provider, query, authors, ids, timeout, **kwargs):
                    if ids and provider == "goodreads":
                        raise TimeoutError("Edition search timed out")
                    if not ids and provider == "open_library":
                        return [deepcopy(candidate)]
                    return []

                with patch.object(self.plugin, "_search", side_effect=search) as lookup:
                    rows = self.identify(
                        title=title, authors=[author], identifiers={"isbn": isbn}
                    )
                self.assertTrue(
                    any(
                        call.args[:4] == ("open_library", title, [author], {})
                        and call.args[4] <= 5
                        for call in lookup.call_args_list
                    )
                )
                if not matches:
                    self.assertEqual(rows, [])
                    continue
                for rank, row in enumerate(rows):
                    row.identify_plugin = self.plugin
                    row.relevance_in_source = rank
                (result,) = merge_identify_results({self.plugin: rows}, ThreadSafeLog())
                self.assertEqual(result.get_identifiers(), {"isbn": isbn})
                self.assertEqual(result.authors, [author])
                self.assertEqual(result.comments, paperback["description"])
                self.assertEqual(result.tags, ["Science Fiction"])
                self.assertIsNone(result.publisher)
                self.assertIsNone(result.pubdate)
                self.assertIsNone(self.plugin._cached_cover(result.get_identifiers()))

    def test_print_fallback_uses_engine_title_author_request(self):
        """Exercise the real Calibre-to-engine request and provider payload shape."""
        self.plugin.prefs["plugin_ids"] = "goodreads"
        (book,) = self.identify(
            title="Star Wars: Knight Errant",
            authors=["John Jackson Miller"],
            identifiers={"isbn": "9780307796530"},
        )
        lookups = [
            query
            for path, query in self.requests
            if path == "/plugins/goodreads/search"
        ]
        self.assertEqual(len(lookups), 2)
        self.assertEqual(lookups[0]["isbn"], ["9780307796530"])
        self.assertNotIn("isbn", lookups[1])
        self.assertEqual(lookups[1]["author"], ["John Jackson Miller"])
        self.assertEqual(book.get_identifiers(), {"isbn": "9780307796530"})
        self.assertEqual(book.tags, ["Science Fiction", "Space Opera"])
        self.assertIn("Kerra Holt", book.comments)

    def test_print_fallback_reuses_a_known_paperback_when_kindle_lookup_misses(self):
        """Use a verified print result already returned by an enabled provider."""
        title = "Star Wars: Knight Errant"
        authors = ["John Jackson Miller"]
        asin = "B0FSKX64P8"
        print_result = {
            "title": title,
            "authors": authors,
            "isbn": "9780345522641",
            "asin": "0345522648",
            "description": "Jedi Kerra Holt confronts the Sith.",
            "tags": ["Science Fiction"],
            "format": "Paperback",
            "publisher": "Del Rey",
        }
        self.plugin.prefs["plugin_ids"] = "hardcover"
        with patch.object(
            self.plugin,
            "_search",
            side_effect=lambda provider, *a, **kw: (
                [deepcopy(print_result)] if provider == "hardcover" else []
            ),
        ) as lookup:
            (result,) = self.identify(
                title=title, authors=authors, identifiers={"mobi-asin": asin}
            )
        self.assertEqual(result.get_identifiers(), {"asin": asin})
        self.assertEqual(result.comments, print_result["description"])
        self.assertEqual(result.tags, print_result["tags"])
        self.assertIsNone(result.publisher)
        self.assertFalse(any(call.args[3] == {} for call in lookup.call_args_list))

    def test_numbered_prefix_recovers_catalog_subjects_and_warning_labels(self):
        """Merge a verified edition and supplemental warnings through real identify."""
        title = "Wicked Darlings 1: A Court of Sugar and Spice"
        base = "A Court of Sugar and Spice"
        authors = ["Rebecca F. Kenney"]
        isbn = "9798362685058"
        referral = (
            "Trigger and content warnings are available on the author's website "
            "and in the front matter of the book."
        )
        sparse = dict(
            title="Wicked Darlings 1",
            subtitle=base,
            authors=authors,
            isbn=isbn,
            description="A synopsis. " + referral,
        )
        self.plugin.prefs["plugin_ids"] = "isbnlab,storygraph"

        def api(url, timeout):
            parsed = urlsplit(url)
            provider = parsed.path.split("/")[-2]
            query = parse_qs(parsed.query)
            self.assertEqual(query["query"], [base])
            if provider == "isbnlab":
                rows = [sparse]
            elif provider == "google_books":
                rows = [
                    dict(
                        sparse,
                        title=base + ": A Nutcracker Romance Retelling",
                        subtitle=None,
                        genres=[
                            "Fiction / Romance / Fantasy",
                            "Fiction / Romance / Holiday",
                        ],
                    )
                ]
            else:
                rows = [
                    dict(
                        title=base,
                        authors=authors,
                        trigger_warnings=["Violence", "Blood"],
                    )
                ]
            return {"results": rows}

        with patch.object(self.plugin, "_http_json", side_effect=api):
            result = self.identify(
                title=title, authors=authors, identifiers={"isbn": isbn}
            )
        matching = [row for row in result if row.get_identifiers().get("isbn") == isbn]
        self.assertEqual(len(matching), 1)
        book = matching[0]
        self.assertIn("Fantasy", book.tags)
        self.assertIn("Holiday", book.tags)
        self.assertEqual(book.series, "Wicked Darlings")
        self.assertEqual(book.series_index, 1.0)
        self.assertTrue(book.comments.startswith("trigger warnings:"), book.comments)
        self.assertIn("Violence", book.comments)
        self.assertNotIn("available on", book.comments)

    def test_numbered_anthology_recovers_series_and_same_edition_subjects(self):
        """Use a discovered ISBN and keep disputed contributor data out of Calibre."""
        heading = "Tales from Alternate Earths 2"
        series = "Tales From Alternate Earths"
        subtitle = "Eleven New Broadcasts from Parallel Dimensions"
        authors = [
            "Daniel M. Bensen",
            "Brent A. Harris",
            "Rob Edwards",
            "Christopher Edwards",
            "Casia Courtier",
            "Jessica Holmes",
            "Gideon Marcus",
            "Jeff Provine",
            "Cindy Tomamichel",
            "Bonnie Milani",
        ]
        isbn = "9781718159075"
        description = (
            "The successor to the Sidewise Award winning Tales From Alternate Earths is here. "
            "Step into the worlds that might have been, with time travel and fairy beings. "
            "All this and more. There are Americas of a different tilt. "
            "Authors Jessica Holmes, Daniel M. Bensen, Rob Edwards, Leo McBride, "
            "Christopher Edwards, Gideon Marcus, Casia Courtier, Jeff Provine, "
            "Cindy Tomamichel, Brent A. Harris and Bonnie Milani show us the world "
            "that might have been."
        )
        row = {
            "title": heading,
            "subtitle": subtitle,
            "authors": authors,
            "isbn": isbn,
            "description": description,
        }
        donor = {
            "title": heading,
            "authors": [*authors[:4], "Casia Pickering", *authors[5:], "Leo McBride"],
            "isbn": isbn,
            "genres": ["Alternate History", "History"],
            "tags": ["goodreads", "audiobooks"],
        }
        self.plugin.prefs["plugin_ids"] = "goodreads"
        goodreads_requests = []

        def api(url, timeout):
            parsed = urlsplit(url)
            provider = parsed.path.split("/")[-2]
            params = parse_qs(parsed.query)
            if provider == "google_books":
                return {"results": [row]}
            goodreads_requests.append(params)
            if params.get("query") != [f"{series} 2"] or params.get("isbn") != [isbn]:
                return {"results": []}
            self.assertNotIn("author", params)
            return {
                "results": [
                    dict(
                        donor,
                        title="Tales from Alternate Earths 3",
                        tags=["Wrong volume"],
                    ),
                    dict(donor, isbn="9798362685058", tags=["Wrong edition"]),
                    dict(donor, authors=["Unrelated Writer"], tags=["Wrong author"]),
                    donor,
                ]
            }

        with patch.object(self.plugin, "_http_json", side_effect=api):
            result = self.identify(
                title=f"{heading}: {subtitle}",
                authors=authors,
                identifiers={"mobi-asin": "B07GBH1YNV"},
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(sum("isbn" not in query for query in goodreads_requests), 1)
        book = result[0]
        self.assertEqual(book.title.casefold(), f"{heading}: {subtitle}".casefold())
        self.assertEqual(book.authors, [*authors, "Leo McBride"])
        self.assertEqual(book.series, series)
        self.assertEqual(book.series_index, 2.0)
        self.assertEqual(
            book.tags,
            [
                "Alternate History",
                "Science Fiction",
                "Fantasy",
                "Anthology",
                "Anthologies",
            ],
        )
        from calibre.library.comments import comments_to_html

        rendered = comments_to_html(book.comments)
        self.assertEqual(rendered.count("<p>"), 4)
        self.assertEqual(
            book.comments.replace("</p><p>", " ")
            .removeprefix("<p>")
            .removesuffix("</p>"),
            description,
        )
        self.assertEqual(
            self.plugin._with_edition_correction(
                dict(row, description=f"<div>\n<p>{description}</p></div>")
            )["description"],
            book.comments,
        )
        unrelated = dict(row, title="Unrelated title", tags=["History"])
        self.assertEqual(self.plugin._with_edition_correction(unrelated), unrelated)
        self.assertIsNone(
            self.plugin._to_metadata(
                dict(row, title="Python 3: A Practical Guide", description="A guide."),
                0,
            ).series
        )

    def test_description_credits_keep_real_and_pen_names(self):
        """Accept explicit credit forms and ignore prose and contributor roles."""
        for description, author, expected in (
            (
                "<p>Authors: Richard Bachman (Stephen King)</p>",
                "Richard Bachman",
                ["Richard Bachman", "Stephen King"],
            ),
            (
                "Stephen King writing as Richard Bachman",
                "Stephen King",
                ["Stephen King", "Richard Bachman"],
            ),
            (
                "<p>Authors: Richard Bachman (Stephen King)</p>",
                "Richard Bachman (Stephen King)",
                ["Richard Bachman", "Stephen King"],
            ),
            (
                "This novel was inspired by Stephen King and Richard Bachman.",
                "Stephen King",
                ["Stephen King"],
            ),
            ("Authors: Stephen King (Editor)", "Stephen King", ["Stephen King"]),
            ("Authors: Stephen King (Richard Bachman)", "Jane Doe", ["Jane Doe"]),
        ):
            with self.subTest(description=description, author=author):
                self.assertEqual(
                    self.plugin._description_authors(description, [author]), expected
                )

    def test_prefix_matching_keeps_identity_and_series_guards(self):
        """A shared ISBN cannot excuse a different author, book, or series number."""
        left = dict(
            title="Wicked Darlings 1: A Court of Sugar and Spice",
            authors=["Rebecca F. Kenney"],
            isbn="9798362685058",
        )
        right = dict(left, title="A Court of Sugar and Spice")
        self.assertTrue(self.plugin._identity_consistent(left, right))
        for wrong in (
            dict(right, authors=["Different Writer"]),
            dict(right, title="Wicked Darlings 2: A Court of Sugar and Spice"),
            dict(right, title="A Court of Hearts and Hunger"),
            dict(right, series=[{"name": "Wicked Darlings", "position": 2}]),
            dict(right, isbn=None),
        ):
            self.assertFalse(self.plugin._identity_consistent(left, wrong))

    def test_missing_warning_details_retry_without_subject_fallback(self):
        """Retry a referral even with tags, without calling an unloaded provider."""
        row = dict(
            title="Example",
            authors=["A Writer"],
            genres=["Fantasy"],
            description="A synopsis. Content warnings are available on the author's website.",
        )
        self.plugin.prefs["plugin_ids"] = "google_books,storygraph"
        calls = []

        def search(provider, query, authors, identifiers, timeout, **kwargs):
            calls.append((provider, identifiers))
            if provider == "storygraph" and not identifiers:
                return [dict(row, content_warnings=["Violence"])]
            return [dict(row)] if provider == "google_books" else []

        with patch.object(self.plugin, "_search", side_effect=search):
            result = self.identify(
                title="Example",
                authors=["A Writer"],
                identifiers={"isbn": "9798362685058"},
            )
        self.assertIn("Violence", result[0].comments)
        self.assertNotIn("available on", result[0].comments)
        self.assertNotIn("tertulia_subjects", [provider for provider, _ in calls])

    def test_inline_warning_list_moves_to_header_without_obfuscation(self):
        """Use explicitly enumerated TW/CW labels, preserving the synopsis."""
        row = dict(
            title="Example",
            authors=["A Writer"],
            description="A synopsis. (TW/CW: family death, vio-lence, go-re, blo-od, se-x-ual threat, light bond-age, light C-NC)",
        )
        book = self.plugin._to_metadata(row, 0)
        self.assertTrue(
            book.comments.startswith(
                "trigger warnings: family death, violence, gore, blood, sexual threat, light bondage, light CNC"
            ),
            book.comments,
        )
        self.assertTrue(book.comments.endswith("A synopsis."))
        self.assertNotIn("TW/CW", book.comments)

    def test_series_prefix_donor_cannot_become_a_catalog_subtitle(self):
        """Keep prefix decoration from poisoning a same-edition title identity."""
        row = dict(
            title="A Court of Sugar and Spice",
            authors=["Rebecca F. Kenney"],
            isbn="9798362685058",
        )
        donor = dict(
            row,
            title="Wicked Darlings 1: A Court of Sugar and Spice",
            series=[{"name": "Wicked Darlings", "position": 1}],
        )
        self.assertTrue(self.plugin._merge_metadata(row, donor))
        self.assertNotIn("subtitle", row)
        self.assertTrue(self.plugin._identity_consistent(row, donor))

    def test_separate_series_heading_and_book_subtitle_match_verified_edition(self):
        """Recognize the provider's actual split of series heading and work title."""
        row = dict(
            title="Wicked Darlings 1",
            subtitle="A Court of Sugar and Spice",
            authors=["Rebecca F. Kenney"],
            isbn="9798362685058",
        )
        donor = dict(
            row,
            title="A Court of Sugar and Spice",
            subtitle="A Nutcracker Romance Retelling",
            series=[{"name": "Wicked Darlings", "position": 1}],
        )
        self.assertTrue(self.plugin._merge_metadata(row, donor))
        metadata = self.plugin._to_metadata(row, 0)
        self.assertEqual(metadata.title, "A Court of Sugar and Spice")
        self.assertEqual(metadata.series, "Wicked Darlings")
        self.assertEqual(metadata.series_index, 1)

    def test_nameless_series_placeholder_does_not_block_prefix_enrichment(self):
        """Ignore default series positions when the provider supplies no series name."""
        title = "Wicked Darlings 1: A Court of Sugar and Spice"
        row = dict(
            title=title,
            authors=["Rebecca F. Kenney"],
            isbn="9798362685058",
            series=[{"name": None, "position": 0}],
        )
        self.assertEqual(self.plugin._series_identity(row), ("", None))
        enriched = self.plugin._with_requested_series(
            row, title, row["authors"], {"isbn": row["isbn"]}
        )
        self.assertEqual(
            enriched["series"], [{"name": "Wicked Darlings", "position": 1.0}]
        )

    def test_source_shape_order_preserves_complete_final_metadata(self):
        """Keep split titles, blank series placeholders and donors order independent."""
        title = "Wicked Darlings 1: A Court of Sugar and Spice"
        authors = ["Rebecca F. Kenney"]
        isbn = "9798362685058"
        referral = "Content warnings are available on the author's website."
        rows = [
            {
                "source": "isbnlab",
                "title": "Wicked Darlings 1",
                "subtitle": "A Court of Sugar and Spice",
                "authors": authors,
                "isbn": isbn,
                "series": [{"name": None, "position": 0}],
                "description": "A Nutcracker romance. " + referral,
            },
            {
                "source": "google_books",
                "title": "A Court of Sugar and Spice",
                "subtitle": "A Nutcracker Romance Retelling",
                "authors": ["Rebecca F Kenney"],
                "isbn": isbn,
                "genres": ["Fiction / Romance / Fantasy"],
            },
            {
                "source": "goodreads",
                "title": "A Court of Sugar and Spice (Wicked Darlings #1)",
                "authors": authors,
                "isbn": isbn,
                "content_warnings": ["Violence", "Blood"],
            },
        ]
        for ordered in permutations(rows):
            with self.subTest(sources=[row["source"] for row in ordered]):
                enriched = [
                    self.plugin._with_requested_series(
                        row, title, authors, {"isbn": isbn}
                    )
                    for row in deepcopy(ordered)
                ]
                # All fields are supplied by compatible initial records. A
                # second network batch must not be needed to repair ordering.
                with patch.object(self.plugin, "_initial_provider_batch") as retry:
                    recovered = self.plugin._recover_supplements(
                        self.log,
                        self.abort,
                        [],
                        enriched,
                        title,
                        authors,
                        {"isbn": isbn},
                        5,
                    )
                retry.assert_not_called()
                final = self.plugin._dedupe(recovered)
                self.assertEqual(len(final), 1)
                book = self.plugin._to_metadata(final[0], 0)
                self.assertEqual(book.series, "Wicked Darlings")
                self.assertEqual(book.series_index, 1.0)
                self.assertIn("Fantasy", book.tags)
                self.assertIn("Violence", book.comments)
                self.assertIn("Blood", book.comments)
                self.assertIsNone(self.plugin._warning_referral(book.comments))

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

    def test_generic_fiction_recovers_horror_and_separates_endorsements(self):
        """Keep the selected edition and render attributed reviews as separate lines."""
        title = "Blood Night: A Brutal 1980s Small-Town Slasher Horror"
        author, asin = "Jonathan Daniel", "B0DJK7WQGQ"
        first_quote = '"The tension builds steadily."'
        second_quote = '"A chilling story."'
        synopsis = "Will anyone survive?"
        html = (
            f"<p>{synopsis}{first_quote}- First Reviewer"
            f"{second_quote} - Second Reviewer<br />More about the book.</p>"
        )
        self.plugin.prefs["plugin_ids"] = "goodreads"
        state = {
            "Book:1": {
                "__typename": "Book",
                "legacyId": 220148316,
                "titleComplete": title,
                "primaryContributorEdge": {"node": {"__ref": "Author:1"}},
                "details": {"asin": asin},
                "bookGenres": [{"genre": {"name": "Horror"}}],
                "description": "This catalog synopsis must stay with its source.",
            },
            "Author:1": {"name": author},
        }
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        for source_tags, catalog_asin, expected_tags, requests in (
            (["Fiction"], asin, ["Fiction", "Horror"], 2),
            (["Fiction"], "B000000001", ["Fiction"], 2),
            (["Fiction", "Horror"], asin, ["Fiction", "Horror"], 0),
        ):
            with self.subTest(tags=source_tags, catalog_asin=catalog_asin):
                # The conflicting ASIN case has no independent genre in the subtitle.
                lookup_title = title if catalog_asin == asin else "Blood Night: A Novel"
                state["Book:1"]["details"]["asin"] = catalog_asin
                state["Book:1"]["titleComplete"] = lookup_title
                data = json.dumps({"props": {"pageProps": {"apolloState": state}}})
                candidate = {
                    "source": "google_books",
                    "title": lookup_title,
                    "authors": [author],
                    "asin": asin,
                    "isbn": "9798991048200",
                    "genres": source_tags,
                    "description": html,
                }

                def search(provider, *args, **kwargs):
                    return [deepcopy(candidate)] if provider == "google_books" else []

                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=[
                            b'[{"bookId": "220148316"}]',
                            f'<script id="__NEXT_DATA__">{data}</script>'.encode(),
                        ],
                    ) as fetch,
                ):
                    rows = self.identify(
                        title=lookup_title,
                        authors=[author],
                        identifiers={"mobi-asin": asin},
                    )
                for rank, row in enumerate(rows):
                    row.identify_plugin = self.plugin
                    row.relevance_in_source = rank
                (result,) = merge_identify_results({self.plugin: rows}, ThreadSafeLog())
                self.assertEqual(set(result.tags), set(expected_tags))
                self.assertEqual(fetch.call_count, requests)
                self.assertEqual(result.get_identifiers()["asin"], asin)
                self.assertEqual(result.get_identifiers()["isbn"], "9798991048200")
                self.assertEqual(
                    result.comments,
                    f"<p>{synopsis}<br /><br />{first_quote} - First Reviewer"
                    f"<br /><br />{second_quote} - Second Reviewer"
                    "<br /><br />More about the book.</p>",
                )

    def test_verified_fiction_subtitle_supplies_genre_during_provider_outage(self):
        """Use the catalog subtitle when the genre provider cannot answer."""
        title = "Blood Night: A Brutal 1980s Small-Town Slasher Horror"
        author, asin = "Jonathan Daniel", "B0DJK7WQGQ"
        self.plugin.prefs["plugin_ids"] = "goodreads"
        for subjects, expected in (
            (["Fiction"], {"Fiction", "Horror"}),
            (["Nonfiction"], {"Nonfiction"}),
        ):
            with self.subTest(subjects=subjects):
                candidate = {
                    "source": "google_books",
                    "title": "Blood Night",
                    "subtitle": title.partition(": ")[2],
                    "authors": [author],
                    "asin": asin,
                    "genres": subjects,
                    "description": "A quiet town faces a masked killer.",
                }

                def search(provider, *args, **kwargs):
                    return [deepcopy(candidate)] if provider == "google_books" else []

                with (
                    patch.object(self.plugin, "_search", side_effect=search),
                    patch.object(self.plugin, "_http_bytes", side_effect=TimeoutError),
                ):
                    (result,) = self.identify(
                        title=title, authors=[author], identifiers={"mobi-asin": asin}
                    )
                self.assertEqual(set(result.tags), expected)
                self.assertEqual(result.get_identifiers()["asin"], asin)

    def test_review_layout_survives_calibre_comment_rendering(self):
        """Check rendered spacing for plain, encoded, and existing HTML reviews."""
        from calibre.library.comments import comments_to_html
        from qt.core import QTextDocument

        cases = (
            (
                'Plot ends. "A chilling story"- Reviewer\nMore plot.',
                '"A chilling story" - Reviewer',
            ),
            (
                "Plot ends. &quot;A chilling story&quot;- Reviewer\nMore plot.",
                '"A chilling story" - Reviewer',
            ),
            (
                "<p>Plot ends.<br>“A chilling story.” — Reviewer<br>More plot.</p>",
                "“A chilling story.” — Reviewer",
            ),
            (
                "<p>Plot ends.<br/><br/>“A chilling story.” — Reviewer<br/><br/>More plot.</p>",
                "“A chilling story.” — Reviewer",
            ),
        )
        for description, review in cases:
            with self.subTest(description=description):
                row = {"title": "Example", "description": description}
                document = QTextDocument()
                document.setHtml(
                    comments_to_html(self.plugin._to_metadata(row, 0).comments)
                )
                self.assertEqual(
                    document.toPlainText(), f"Plot ends.\n\n{review}\n\nMore plot."
                )
        dialogue = "<p>“Don’t go,” she whispered, then left.</p>"
        self.assertEqual(
            self.plugin._to_metadata(
                {"title": "Example", "description": dialogue}, 0
            ).comments,
            dialogue,
        )
        blocks = "<p>Plot ends.</p><blockquote>“A chilling story.” — Reviewer</blockquote><p>More plot.</p>"
        self.assertEqual(
            self.plugin._to_metadata(
                {"title": "Example", "description": blocks}, 0
            ).comments,
            blocks,
        )

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

    def test_requested_edition_inherits_subjects_from_the_same_work(self):
        """Return the requested edition with work tags from the other format."""
        title = "Pumpkin Spice & Poltergeist: A cozy sapphic paranormal rom-com"
        authors = ["Ali K. Mulford", "K. Elle Morrison"]
        isbn, asin = "9781923184145", "B0D6R3GNS8"
        base, subtitle = title.split(": ", 1)
        edition = dict(
            title=base,
            subtitle=subtitle,
            authors=authors,
            isbn=isbn,
            asin=asin,
            source="hardcover",
            series=[{"name": "Maple Hollow", "index": 1}],
            tags=["Romance"],
            description="Edition synopsis.",
            confidence=0.5,
        )
        paperback = dict(
            title=title + " (Maple Hollow)",
            authors=authors,
            isbn="9781923184176",
            source="goodreads",
            source_id="218151495",
            tags=["Fantasy", "Witches"],
            publisher="Paperback Publisher",
            narrators=["spook louder, clearer"],
            metadata_type="audiobooks",
            extra={"format": "Paperback"},
            confidence=1.0,
        )
        wrong = dict(
            paperback, title=title + " (Maple Hollow Book 2)", tags=["Wrong volume"]
        )
        self.plugin.prefs["plugin_ids"] = "hardcover"
        with patch.object(
            self.plugin,
            "_search",
            side_effect=lambda *a, **k: deepcopy([paperback, wrong, edition]),
        ):
            (result,) = self.identify(
                title=title + " (Maple Hollow Book 1)",
                authors=authors,
                identifiers={"isbn": isbn, "mobi-asin": asin},
            )
        self.assertEqual((result.series, result.series_index), ("Maple Hollow", 1.0))
        self.assertEqual(result.tags, ["Romance", "Fantasy", "Witches"])
        self.assertEqual(result.comments, "Edition synopsis.")
        self.assertEqual(result.get_identifiers()["isbn"], isbn)
        self.assertEqual(result.get_identifiers()["asin"], asin)
        self.assertNotIn("goodreads", result.get_identifiers())
        self.assertIsNone(result.publisher)
        self.assertNotIn(
            "Narrator(s)", self.plugin._to_metadata(paperback, 0).comments or ""
        )

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
        self.assertCountEqual(
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

    def test_cover_shapes_survive_the_calibre_identify_and_cover_handoff(self):
        """Offer only the preferred available aspect to Calibre's area sorter."""
        from calibre.ebooks.metadata.sources.covers import Worker
        from calibre.ebooks.metadata.sources.identify import merge_identify_results
        from calibre.utils.logging import ThreadSafeLog

        self.plugin.prefs["plugin_ids"] = "cover-shapes"
        for title, expected in (
            ("Book Shapes", (250, 400)),
            ("Comic Shapes", (250, 400)),
            ("Audio Shapes", (200, 200)),
            ("Audio Fallback", (1200, 800)),
        ):
            with self.subTest(title=title):
                self.plugin._cover_urls.clear()
                identifiers = {"isbn": "9780547928227"}
                (book,) = self.identify(
                    title=title, authors=["A Writer"], identifiers=identifiers
                )
                book.identify_plugin = self.plugin
                book.relevance_in_source = 0
                (merged,) = merge_identify_results(
                    {self.plugin: [book]}, ThreadSafeLog()
                )
                saved = merged.get_identifiers()
                self.assertEqual(
                    saved.get("metadata_engine_media"),
                    "audiobook" if title.startswith("Audio") else None,
                )
                for cover_ids in (saved, identifiers):
                    queue = Queue()
                    worker = Worker(
                        self.plugin,
                        Event(),
                        merged.title,
                        merged.authors,
                        cover_ids,
                        5,
                        queue,
                    )
                    worker.start()
                    worker.join(10)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(
                        {
                            struct.unpack("!II", data[16:24])
                            for _, data in list(queue.queue)
                        },
                        {expected},
                    )

    def test_direct_kindle_cover_beats_smaller_isbn_cover(self):
        """Pick the larger verified Kindle cover even when an ISBN is present."""
        asin = "B07GBH1YNV"
        isbn = "9781718159075"
        kindle_url = f"https://ec2.images-amazon.com/images/P/{asin}.01.MAIN._SCRM_.jpg"
        title = "Tales from Alternate Earths 2"
        authors = ["Daniel M. Bensen"]
        catalog_url = "https://example.test/isbn-cover.png"
        images = {catalog_url: png(333, 500), kindle_url: png(1563, 2180)}
        catalog = {
            "title": title,
            "authors": authors,
            "isbn": isbn,
            "cover_url": catalog_url,
        }
        self.plugin.prefs["plugin_ids"] = "fixture"
        for identifiers in (
            {"isbn": isbn, "mobi-asin": asin},
            {"isbn": isbn, "amazon_uk": asin},
            {"isbn": isbn},
        ):
            with self.subTest(identifiers=identifiers):
                self.plugin._cover_urls.clear()
                rows = (
                    [dict(catalog, asin=asin)]
                    if len(identifiers) == 1
                    else [
                        catalog,
                        dict(
                            catalog,
                            asin="B000000001",
                            cover_url="https://example.test/conflicting-asin.png",
                        ),
                    ]
                )
                queue = Queue()
                with (
                    patch.object(self.plugin, "_search", return_value=rows),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=lambda url, _: images[url],
                    ) as download,
                ):
                    self.plugin.download_cover(
                        self.log,
                        queue,
                        self.abort,
                        title=title,
                        authors=authors,
                        identifiers=identifiers,
                        get_best_cover=True,
                    )
                self.assertEqual(queue.get_nowait()[1], images[kindle_url])
                self.assertTrue(queue.empty())
                self.assertEqual(
                    {call.args[0] for call in download.call_args_list}, set(images)
                )
                self.assertEqual(
                    self.plugin._cached_cover({"mobi-asin": asin}), kindle_url
                )

    def test_apple_cover_requests_native_dimensions(self):
        """Use Apple's ISBN result and retain its original artwork as fallback."""
        isbn = "9780547951973"
        base = "https://is1-ssl.mzstatic.com/image/thumb/Publication/cover.jpg/"
        original = base + "100x100bb.jpg"
        native = base + "10000x10000bb.jpg"
        lookup = (
            "https://itunes.apple.com/lookup?"
            f"isbn={isbn}&entity=ebook&limit=10&version=2&country=US"
        )
        ebook = {
            "kind": "ebook",
            "trackName": "The Hobbit",
            "artistName": "J. R. R. Tolkien",
            "artworkUrl100": original,
        }
        wrong = dict(
            ebook, trackName="A Different Book", artworkUrl100=base + "wrong.jpg"
        )
        payload = json.dumps({"results": [wrong, ebook]}).encode()
        small = png(67, 100)
        full = png(1600, 2400)
        self.plugin.prefs["plugin_ids"] = "itunes"
        for native_data, expected in ((full, full), (b"invalid", small)):
            with self.subTest(native_available=expected == full):
                self.plugin._cover_urls.clear()
                images = {lookup: payload, native: native_data, original: small}
                queue = Queue()
                with (
                    patch.object(self.plugin, "_search", return_value=[]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=lambda url, _: images[url],
                    ) as fetch,
                ):
                    self.plugin.download_cover(
                        self.log,
                        queue,
                        Event(),
                        title=ebook["trackName"],
                        authors=[ebook["artistName"]],
                        identifiers={"isbn": isbn},
                        get_best_cover=True,
                    )
                self.assertEqual(queue.get_nowait()[1], expected)
                self.assertEqual(
                    {call.args[0] for call in fetch.call_args_list}, set(images)
                )

    def test_apple_cover_accepts_reversed_franchise_title_for_same_author(self):
        """Validate the exact ISBN cover when Apple reverses the two title labels."""
        target = {
            "title": "Star Wars: Knight Errant",
            "authors": ["John Jackson Miller"],
            "isbn": "9780345522641",
        }
        apple = {
            "kind": "ebook",
            "trackName": "Knight Errant: Star Wars",
            "artistName": "John Jackson Miller",
            "artworkUrl100": "https://is1-ssl.mzstatic.com/knight-errant.jpg",
        }
        with patch.object(
            self.plugin,
            "_http_json",
            return_value={
                "results": [
                    dict(apple, artistName="Another Writer"),
                    dict(apple, trackName="Knight Errant: Star Trek"),
                    apple,
                ]
            },
        ):
            (cover,) = self.plugin._apple_cover_candidates(target, 5)
        self.assertEqual(cover["cover_url"], apple["artworkUrl100"])
        self.assertEqual(cover["isbn"], target["isbn"])

    def test_google_cover_uses_saved_volume_id(self):
        """Rank native Google artwork and retry the lower zoom when needed."""
        google_id = "pD6arNyKyi8C"
        isbn = "9780547951973"
        title = "The Hobbit"
        authors = ["J. R. R. Tolkien"]
        frontcover = f"https://books.google.com/books?id={google_id}&printsec=frontcover&img=1&zoom="
        original = "https://example.test/thumbnail.png"
        catalog = dict(title=title, authors=authors, isbn=isbn, cover_url=original)
        full = png(1744, 2616)
        fallback = png(500, 750)
        self.plugin.prefs["plugin_ids"] = "fixture"
        for image, expected in ((full, full), (b"invalid", fallback)):
            with self.subTest(native_available=image == full):
                self.plugin._cover_urls.clear()
                queue = Queue()
                images = {original: png(317, 475), frontcover + "0": image}
                if image != full:
                    images[frontcover + "1"] = fallback
                with (
                    patch.object(self.plugin, "_search", return_value=[catalog]),
                    patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=lambda url, _: images[url],
                    ) as fetch,
                ):
                    self.plugin.download_cover(
                        self.log,
                        queue,
                        Event(),
                        title=title,
                        authors=authors,
                        identifiers={
                            "isbn": isbn,
                            "metadata_engine_google_books": google_id,
                        },
                        get_best_cover=True,
                    )
                self.assertEqual(queue.get_nowait()[1], expected)
                self.assertEqual(
                    {call.args[0] for call in fetch.call_args_list}, set(images)
                )

    def test_calibre_cover_uses_asin_from_merged_identify_result(self):
        """A verified ISBN-only provider result must retain the input Kindle ASIN."""
        from calibre.ebooks.metadata.sources.identify import identify
        from calibre.utils.logging import DevNull

        asin = "B07GBH1YNV"
        title = "Tales from Alternate Earths 2"
        authors = ["Daniel M. Bensen"]
        isbn = "9781718159075"
        kindle_url = f"https://ec2.images-amazon.com/images/P/{asin}.01.MAIN._SCRM_.jpg"
        catalog_url = "https://example.test/isbn-cover.png"
        catalog = {
            "title": title,
            "authors": authors,
            "isbn": isbn,
            "description": "An alternate history anthology.",
            "tags": ["Alternate History"],
            "cover_url": catalog_url,
        }
        images = {catalog_url: png(333, 500), kindle_url: png(1563, 2180)}
        self.plugin.prefs["plugin_ids"] = "fixture"
        for provider_asin in (None, "B000000001"):
            with self.subTest(provider_asin=provider_asin):
                row = dict(catalog, asin=provider_asin)
                with patch.object(self.plugin, "_search", return_value=[row]):
                    books = identify(
                        DevNull(),
                        Event(),
                        title=title,
                        authors=authors,
                        identifiers={"mobi-asin": asin},
                        timeout=5,
                        allowed_plugins=["Metadata Engine"],
                    )
                    self.assertEqual(len(books), 1)
                    book = books[0]
                    identifiers = book.get_identifiers()
                    self.assertEqual(identifiers.get("asin"), provider_asin or asin)
                    if provider_asin:
                        continue
                    queue = Queue()
                    with patch.object(
                        self.plugin,
                        "_http_bytes",
                        side_effect=lambda url, _: images[url],
                    ):
                        self.plugin.download_cover(
                            self.log,
                            queue,
                            Event(),
                            title=book.title,
                            authors=book.authors,
                            identifiers=identifiers,
                            get_best_cover=True,
                        )
                    self.assertEqual(queue.get_nowait()[1], images[kindle_url])

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
