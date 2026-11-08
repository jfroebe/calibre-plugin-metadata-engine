"""Validate reported books against the working-test engine in real Calibre."""

from __future__ import annotations

import logging
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
    cases = [
        {
            "title": "Spice travels : a spice merchant's voyage of discovery",
            "authors": ["Ian Rupert"], "isbn": "0732911516",
            "canonical_isbn": "9780732911515",
            "expected_authors": ["Ian Rupert Hemphill"],
            "minimum_description": 80, "topic": "spice",
        },
        {
            "title": "Smoke & Spice",
            "authors": ["Cheryl Alters Jamison"], "isbn": "9781558328365",
            "canonical_isbn": "9781558328365",
            "expected_authors": ["Cheryl Alters Jamison", "Bill Jamison"],
            "minimum_description": 600, "topic": "barbecue",
        },
    ]
    for case in cases:
        queue = Queue()
        error = plugin.identify(
            logging.getLogger("live-quality"), queue, Event(),
            title=case["title"], authors=case["authors"],
            identifiers={"isbn": case["isbn"]}, timeout=15,
        )
        if error:
            raise AssertionError(error)
        matching = [
            row for row in list(queue.queue)
            if plugin._canonical_isbn(row.get_identifiers().get("isbn"))
            == case["canonical_isbn"]
        ]
        assert len(matching) == 1, "Expected one verified result for " + case["title"]
        book = matching[0]
        assert book.authors == case["expected_authors"], book.authors
        description = plugin._usable_description(book.comments)
        assert description and len(description) >= case["minimum_description"], "No full live synopsis for " + case["title"]
        assert case["topic"] in description.casefold(), "Synopsis has the wrong subject"
        assert book.tags, "Missing final subject tags"
        assert not any(
            tag.casefold() in {"archive.org", "inlibrary", "printdisabled", "work"}
            for tag in book.tags
        )
        print("LIVE_METADATA_QUALITY_PASS title=" + book.title)
        print("LIVE_DESCRIPTION_CHARACTERS=" + str(len(description)))
        print("LIVE_SUBJECTS=" + repr(book.tags))


if __name__ == "__main__":
    main()
