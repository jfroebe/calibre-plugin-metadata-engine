"""Validate reported books against the working-test engine in real Calibre."""

from __future__ import annotations

import json
import logging
import time
from queue import Queue
from threading import Event
from urllib.request import ProxyHandler, build_opener, install_opener

from calibre.customize.ui import metadata_plugins


def main():
    """Require correct identities, full synopses and useful final subjects."""
    install_opener(build_opener(ProxyHandler({})))
    logging.basicConfig(level=logging.INFO)
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


if __name__ == "__main__":
    main()
