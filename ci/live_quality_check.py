"""Validate reported books against the working-test engine in real Calibre."""

from __future__ import annotations

import json
import logging
import os
import time
from hashlib import sha256
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from urllib.request import urlopen

from calibre.customize.ui import add_plugin, metadata_plugins
from qt.core import QImage


def install_kindle_reference():
    """Install the published, pinned Kindle hi-res plugin in disposable Calibre."""
    with urlopen(
        "https://plugins.calibre-ebook.com/286970.zip", timeout=20
    ) as response:
        archive = response.read()
    if sha256(archive).hexdigest() != (
        "592f0322a1697229c15d1b93b2095e60abf66fa5c84a238acefba443d8a4ae94"
    ):
        raise AssertionError("Kindle hi-res covers 0.5.1 archive checksum mismatch")
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "kindle-hi-res.zip"
        path.write_bytes(archive)
        add_plugin(str(path))
    return next(
        p for p in metadata_plugins(("cover",)) if p.name == "Kindle hi-res covers"
    )


def check_cover_quality(engine, kindle, book, case):
    """Fail when the best Metadata Engine cover loses to Kindle on pixel area."""
    identifiers = {**book.get_identifiers(), **case["identifiers"]}
    log = logging.getLogger("live-cover-quality")

    def sizes(plugin, best_only):
        queue = Queue()
        plugin.download_cover(
            log,
            queue,
            Event(),
            title=book.title,
            authors=book.authors,
            identifiers=identifiers,
            timeout=case.get("timeout", 30),
            get_best_cover=best_only,
        )
        images = [QImage.fromData(data) for _, data in list(queue.queue)]
        return [
            (image.width(), image.height())
            for image in images
            if not image.isNull() and min(image.width(), image.height()) >= 100
        ]

    def pixels(size):
        return size[0] * size[1]

    reference = max(sizes(kindle, False), key=pixels, default=None)
    if reference is None:
        if case.get("require_kindle_cover"):
            raise AssertionError(
                "Known Kindle reference cover unavailable: " + book.title
            )
        print("LIVE_KINDLE_COVER_UNAVAILABLE=" + book.title, flush=True)
        return
    actual = max(sizes(engine, True), key=pixels, default=None)
    print(
        f"LIVE_COVER_COMPARISON title={book.title!r} engine={actual} kindle={reference}",
        flush=True,
    )
    if actual is None or pixels(actual) < pixels(reference):
        raise AssertionError(
            f"Metadata Engine cover {actual} is smaller than Kindle {reference}: {book.title}"
        )


def main():
    """Require correct identities, full synopses and useful final subjects."""
    # Only bypass the relay for the LAN engine. External cover sources use it.
    for key in ("no_proxy", "NO_PROXY"):
        os.environ[key] = ",".join(filter(None, (os.environ.get(key), "192.168.0.59")))
    logging.basicConfig(level=logging.INFO)
    kindle = install_kindle_reference()
    plugin = next(
        p for p in metadata_plugins(("identify",)) if p.name == "Metadata Engine"
    )
    plugin.prefs["base_url"] = "http://192.168.0.59:14000"
    plugin.prefs["plugin_ids"] = ""
    recover = plugin._recover_supplements

    def inspect_supplements(*args):
        rows = args[3]
        same_isbn = [
            row
            for row in rows
            if plugin._canonical_isbn(row.get("isbn")) == "9798362685058"
        ]
        for row in same_isbn:
            print(
                "LIVE_RAW_IDENTITY="
                + json.dumps(
                    {
                        key: row.get(key)
                        for key in (
                            "source",
                            "title",
                            "subtitle",
                            "authors",
                            "isbn",
                            "asin",
                            "series",
                            "published_year",
                            "page_count",
                        )
                    }
                )
            )
        for left in same_isbn:
            for right in same_isbn:
                if left is not right:
                    print(
                        "LIVE_PAIR="
                        + repr(
                            (
                                left.get("source"),
                                right.get("source"),
                                plugin._compatible_identifiers(left, right),
                                plugin._identity_consistent(left, right),
                                plugin._work_description_matches(left, right),
                            )
                        )
                    )
        return recover(*args)

    plugin._recover_supplements = inspect_supplements
    cases = [
        {
            "title": "Wicked Darlings 1: A Court of Sugar and Spice",
            "authors": ["Rebecca F. Kenney"],
            "isbn": "9798362685058",
            "identifiers": {
                "barnesnoble": "1142815476",
                "google": "bIeLzwEACAAJ",
                "mobi-asin": "B0BLS8QDJZ",
            },
            "canonical_isbn": "9798362685058",
            "expected_authors": ["Rebecca F. Kenney"],
            "minimum_description": 500,
            "expected_subjects": {"fantasy", "romance"},
            "topic": "nutcracker",
            "warnings": True,
            "series": ("Wicked Darlings", 1.0),
        },
        {
            "title": "Spice travels : a spice merchant's voyage of discovery",
            "authors": ["Ian Rupert"],
            "isbn": "0732911516",
            "canonical_isbn": "9780732911515",
            "expected_authors": ["Ian Rupert Hemphill"],
            "minimum_description": 80,
            "expected_subjects": {"spice trade", "condiments"},
            "topic": "spice",
        },
        {
            "title": "Smoke & Spice",
            "authors": ["Cheryl Alters Jamison"],
            "isbn": "9781558328365",
            "canonical_isbn": "9781558328365",
            "expected_authors": ["Cheryl Alters Jamison", "Bill Jamison"],
            "minimum_description": 600,
            "expected_subjects": {"cooking", "cookbooks"},
            "topic": "barbecue",
        },
        {
            "title": "Tales From Alternate Earths 2: Eleven new broadcasts from parallel dimensions",
            "authors": [
                "Daniel M Bensen",
                "Brent A. Harris",
                "Rob Edwards",
                "Christopher Edwards",
                "Casia Courtier",
                "Jessica Holmes",
                "Gideon Marcus",
                "Jeff Provine",
                "Cindy Tomamichel",
                "Bonnie Milani",
            ],
            "identifiers": {"mobi-asin": "B07GBH1YNV"},
            "canonical_isbn": "9781718159075",
            "additional_authors": ["Leo McBride"],
            "minimum_description": 500,
            "expected_subjects": {"alternate history"},
            "exact_tags": [
                "Alternate History",
                "Science Fiction",
                "Fantasy",
                "Anthology",
                "Anthologies",
            ],
            "paragraphs": 4,
            "require_kindle_cover": True,
            "topic": "alternate history",
            "series": ("Tales From Alternate Earths", 2.0),
            "timeout": 30,
        },
    ]
    for case in cases:
        queue = Queue()
        started = time.monotonic()
        error = plugin.identify(
            logging.getLogger("live-quality"),
            queue,
            Event(),
            title=case["title"],
            authors=case["authors"],
            identifiers={
                **({"isbn": case["isbn"]} if "isbn" in case else {}),
                **case.get("identifiers", {}),
            },
            timeout=case.get("timeout", 15),
        )
        if error:
            raise AssertionError(error)
        print(f"LIVE_LOOKUP_SECONDS={time.monotonic() - started:.2f}", flush=True)
        matching = [
            row
            for row in list(queue.queue)
            if plugin._canonical_isbn(row.get_identifiers().get("isbn"))
            == case["canonical_isbn"]
        ]
        if len(matching) != 1:
            raise AssertionError(
                "Expected one verified result for "
                + case["title"]
                + "; got "
                + repr(
                    [
                        (
                            row.title,
                            row.authors,
                            row.tags,
                            row.get_identifiers(),
                            row.series,
                            row.series_index,
                        )
                        for row in matching
                    ]
                )
            )
        book = matching[0]
        expected_authors = case.get("expected_authors")
        if expected_authors and book.authors != expected_authors:
            raise AssertionError(book.authors)
        if not expected_authors:
            actual = {plugin._author_words(person) for person in book.authors}
            expected = {
                plugin._author_words(person)
                for person in [*case["authors"], *case.get("additional_authors", [])]
            }
            if actual != expected:
                raise AssertionError(book.authors)
        description = plugin._usable_description(book.comments)
        if not (description and len(description) >= case["minimum_description"]):
            raise AssertionError("No full live synopsis for " + case["title"])
        if case["topic"] not in description.casefold():
            raise AssertionError("Synopsis has the wrong subject")
        if not book.tags:
            raise AssertionError("Missing final subject tags")
        subjects = {tag.casefold() for tag in book.tags}
        if not case["expected_subjects"].intersection(subjects):
            raise AssertionError("No relevant subjects for " + case["title"])
        if case.get("exact_tags") and book.tags != case["exact_tags"]:
            raise AssertionError(book.tags)
        if case.get("paragraphs") and description.count("<p>") != case["paragraphs"]:
            raise AssertionError("Missing synopsis paragraphs for " + case["title"])
        if case.get("warnings"):
            if "trigger warnings:" not in book.comments.casefold():
                raise AssertionError(book.comments)
            if plugin._warning_referral(book.comments):
                raise AssertionError("Live metadata requirement failed")
            labels = book.comments.splitlines()[0].partition(":")[2].strip()
            if not labels or not any(label.isalpha() for label in labels):
                raise AssertionError("Missing actual warning labels")
        if case.get("series") and (book.series, book.series_index) != case["series"]:
            raise AssertionError((book.series, book.series_index))
        if any(
            tag.casefold() in {"archive.org", "inlibrary", "printdisabled", "work"}
            for tag in book.tags
        ):
            raise AssertionError("Live metadata requirement failed")
        print("LIVE_METADATA_QUALITY_PASS title=" + book.title)
        print("LIVE_DESCRIPTION_CHARACTERS=" + str(len(description)))
        print("LIVE_SUBJECTS=" + repr(book.tags))
        if case.get("warnings"):
            print("LIVE_COMMENTS=" + book.comments)
        if case.get("identifiers", {}).get("mobi-asin"):
            check_cover_quality(plugin, kindle, book, case)


if __name__ == "__main__":
    main()
