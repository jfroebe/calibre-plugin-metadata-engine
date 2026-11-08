"""Validate the reported book against the working-test engine in real Calibre."""

from __future__ import annotations

import logging
from queue import Queue
from threading import Event
from urllib.request import ProxyHandler, build_opener, install_opener

from calibre.customize.ui import metadata_plugins


def main():
    """Require a correct identified book and useful final metadata from live providers."""
    install_opener(build_opener(ProxyHandler({})))
    logging.basicConfig(level=logging.INFO)
    plugin = next(
        p for p in metadata_plugins(("identify",)) if p.name == "Metadata Engine"
    )
    plugin.prefs["base_url"] = "http://192.168.0.59:14000"
    plugin.prefs["plugin_ids"] = ""
    queue = Queue()
    error = plugin.identify(
        logging.getLogger("live-quality"),
        queue,
        Event(),
        title="Spice travels : a spice merchant's voyage of discovery",
        authors=["Ian Rupert"],
        identifiers={"isbn": "0732911516"},
        timeout=15,
    )
    if error:
        raise AssertionError(error)
    results = list(queue.queue)
    matching = [
        row
        for row in results
        if plugin._canonical_isbn(row.get_identifiers().get("isbn"))
        == "9780732911515"
    ]
    assert matching, "The reported ISBN was not identified"
    book = matching[0]
    assert book.authors == ["Ian Rupert Hemphill"], book.authors
    description = plugin._usable_description(book.comments)
    assert description and len(description) >= 80, "No substantive live synopsis"
    assert "spice" in description.casefold(), "Synopsis has the wrong subject"
    assert book.tags, "Missing final subject tags"
    assert any(
        "spice" in tag.casefold() or "condiment" in tag.casefold()
        for tag in book.tags
    )
    assert not any(
        tag.casefold() in {"archive.org", "inlibrary", "printdisabled", "work"}
        for tag in book.tags
    )
    print("LIVE_METADATA_QUALITY_PASS title=" + book.title)
    print("LIVE_DESCRIPTION_CHARACTERS=" + str(len(description)))
    print("LIVE_SUBJECTS=" + repr(book.tags))


if __name__ == "__main__":
    main()
