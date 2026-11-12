"""Validate reported books against the working-test engine in real Calibre."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from hashlib import sha256
from html import unescape
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from urllib.request import urlopen

from calibre.customize.ui import add_plugin, all_metadata_plugins, metadata_plugins
from calibre.ebooks.metadata.sources.base import Source
from calibre.ebooks.metadata.sources.covers import Worker
from calibre.ebooks.metadata.sources.identify import merge_identify_results
from calibre.utils.logging import ThreadSafeLog
from qt.core import QImage

COVER_REFERENCES = {
    "Apple Books covers": (
        "362859",
        "e3543ccc0455f476b1f02ebce6b68818a2e999cd05b5dba824f8d0224b4979bf",
    ),
    "Goodreads": (
        "130638",
        "a940b53392e9b14768d464c2bcdf0857a17652a42b3e8239b85862d27329eaf5",
    ),
    "Kindle hi-res covers": (
        "286970",
        "592f0322a1697229c15d1b93b2095e60abf66fa5c84a238acefba443d8a4ae94",
    ),
}


def install_cover_references():
    """Install published, checksum-pinned plugins in disposable Calibre."""
    with TemporaryDirectory() as temporary:
        for name, (archive_id, digest) in COVER_REFERENCES.items():
            url = f"https://plugins.calibre-ebook.com/{archive_id}.zip"
            with urlopen(url, timeout=20) as response:
                archive = response.read()
            if sha256(archive).hexdigest() != digest:
                raise AssertionError(f"{name} archive checksum mismatch")
            path = Path(temporary) / f"{archive_id}.zip"
            path.write_bytes(archive)
            add_plugin(str(path))
    # Calibre's Google Books source implements cover download but currently
    # advertises only identify. Include implemented downloaders as well.
    sources = [
        source
        for source in all_metadata_plugins()
        if "cover" in source.capabilities
        or type(source).download_cover is not Source.download_cover
    ]
    missing = ({"Google"} | COVER_REFERENCES.keys()) - {p.name for p in sources}
    if missing:
        raise AssertionError(f"Missing cover comparison plugins: {sorted(missing)}")
    return sources


def cover_quality(size, audiobook):
    """Compare aspect first, then decoded pixel area within the same aspect."""
    width, height = size
    if audiobook:
        shape = 2 if width == height else int(width > height)
    else:
        shape = 2 if height > width else 0
    return shape, width * height


def check_review_blocks(description, credits):
    """Require returned endorsements to occupy standalone comment blocks."""
    blocks = re.split(
        r"(?:<br\s*/?>\s*){2,}|</(?:p|blockquote|div)>|\n\s*\n",
        description,
        flags=re.IGNORECASE,
    )
    text_blocks = [unescape(re.sub(r"<[^<>]+>", "", block)).strip() for block in blocks]
    for credit in credits:
        # Providers may publish a synopsis without promotional reviews.
        if credit not in unescape(description):
            continue
        matches = [block for block in text_blocks if credit in block]
        if len(matches) != 1 or not re.fullmatch(
            r'["“][^"“”]+["”]\s*[-–—]\s*' + re.escape(credit) + r'[^"“”]*',
            matches[0],
        ):
            raise AssertionError(f"Review for {credit} is not a standalone block")


def check_cover_quality(engine, sources, book, case, stalled_sources):
    """Compare cover results from Calibre workers using only merged identifiers."""
    book.identify_plugin = engine
    book.relevance_in_source = 0
    (merged,) = merge_identify_results({engine: [book]}, ThreadSafeLog())
    identifiers = merged.get_identifiers()
    audiobook = identifiers.get("metadata_engine_media") == "audiobook"

    def quality(size):
        return cover_quality(size, audiobook)

    expected_asin = case.get("identifiers", {}).get("mobi-asin")
    if expected_asin and engine._cover_asin(identifiers) != expected_asin:
        raise AssertionError(
            f"Identify result lost Kindle ASIN {expected_asin}: {merged.title}"
        )

    workers = {}
    report = {}
    timeout = case.get("timeout", 30)
    deadline = time.monotonic() + timeout + 25
    for source in sources:
        if source.name in stalled_sources or not source.is_configured():
            report[source.name] = {
                "status": "previous_timeout"
                if source.name in stalled_sources
                else "unconfigured"
            }
            continue
        worker = Worker(
            source,
            Event(),
            merged.title,
            list(merged.authors),
            dict(identifiers),
            timeout,
            Queue(),
            get_best_cover=(
                source.name == engine.name
                or source.name in case.get("isbn_first_covers", ())
            ),
        )
        workers[source.name] = worker
        worker.start()

    for name, worker in workers.items():
        worker.join(max(0, deadline - time.monotonic()))
        if worker.is_alive():
            worker.abort.set()
            stalled_sources.add(name)
        with worker.rq.mutex:
            downloads = list(worker.rq.queue)
        images = [QImage.fromData(data) for _, data in downloads]
        sizes = [
            (image.width(), image.height())
            for image in images
            if not image.isNull() and min(image.width(), image.height()) >= 100
        ]
        report[name] = {
            "status": (
                "timeout"
                if name in stalled_sources
                else "returned"
                if sizes
                else "error"
                if "Traceback" in worker.buf.getvalue()
                else "no_cover"
            ),
            "size": max(sizes, key=quality, default=None),
            "seconds": worker.time_spent,
        }

    # Image searches may return artwork unrelated to this edition. Record their
    # results, but use only catalog sources in the size gate.
    catalog_names = {
        source.name
        for source in sources
        if source.name != engine.name
        and (
            "identify" in source.capabilities
            or source.name in COVER_REFERENCES
            or source.name == "Open Library"
        )
        and source.name != "Google Images"
    }
    actual = report[engine.name].get("size")
    print(
        "LIVE_COVER_COMPARISON="
        + json.dumps(
            {
                "title": merged.title,
                "identifiers": identifiers,
                "catalog_references": sorted(catalog_names),
                "sources": report,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    failures = []
    for name in case.get("required_covers", ()):
        if not report[name].get("size"):
            failures.append(f"Known {name} reference unavailable: {merged.title}")
    for name in sorted(catalog_names):
        reference = report[name].get("size")
        if reference and (actual is None or quality(actual) < quality(reference)):
            failures.append(
                f"{merged.title}: Metadata Engine {actual} ranks below {name} {reference}"
            )
    if case.get("portrait_cover") and (actual is None or actual[0] >= actual[1]):
        failures.append(f"{merged.title}: expected a portrait cover, received {actual}")
    return failures


def main():
    """Require correct identities, full synopses and useful final subjects."""
    # Only bypass the relay for the LAN engine. External cover sources use it.
    for key in ("no_proxy", "NO_PROXY"):
        os.environ[key] = ",".join(filter(None, (os.environ.get(key), "192.168.0.59")))
    logging.basicConfig(level=logging.INFO)
    cover_sources = install_cover_references()
    stalled_sources = set()
    cover_failures = []
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
            "title": "Henrikk's Punishment",
            "authors": ["Darcy Fayton"],
            "identifiers": {
                "amazon": "B0CVZJHQ6W",
                "goodreads": "208896222",
                "mobi-asin": "B0CVZJHQ6W",
            },
            "expected_title": "Henrikk's Punishment",
            "expected_catalog": ("metadata_engine_book_notification", "3886230"),
            "series": ("Henrikk's Short Stories", 1.0),
            "one_result": True,
            "timeout": 30,
        },
        {
            "title": "Bedded by the Headmaster",
            "authors": ["Darcy Fayton"],
            "expected_goodreads": "217994670",
            "expected_title": "Bedded by the Headmaster",
            "one_result": True,
            "metadata_only": True,
            "series": ("Susie & Arken's Duet", 1.0),
            "expected_subjects": {"paranormal romance", "fantasy", "vampires"},
            "required_subjects": {"paranormal romance", "vampires"},
            "minimum_description": 400,
            "topic": "volmasque academy",
            "timeout": 30,
        },
        {
            "title": "Branding Belle: A Reverse Harem Contemporary Standalone",
            "authors": ["Crystal North", "Kira Roman"],
            "identifiers": {"mobi-asin": "B096HHSWWR"},
            "expected_goodreads": "58325014",
            "expected_title": "Branding Belle",
            "one_result": True,
            "no_series": True,
            "expected_subjects": {"romance", "contemporary romance"},
            "required_subjects": {"contemporary romance"},
            "minimum_description": 300,
            "topic": "tattoo",
            "timeout": 30,
        },
        {
            "title": "Blood Night: A Brutal 1980s Small-Town Slasher Horror",
            "authors": ["Jonathan Daniel"],
            "identifiers": {"mobi-asin": "B0DJK7WQGQ"},
            "canonical_isbn": "9798991048200",
            "expected_subjects": {"horror"},
            "required_subjects": {"horror"},
            "review_credits": ("Felix Blackwell", "HorrorNews"),
            "merge_identify": True,
            "minimum_description": 400,
            "topic": "elden mills",
            "timeout": 30,
        },
        {
            "title": "Supers",
            "authors": ["Patrick Stewart"],
            "identifiers": {"mobi-asin": "B088T3V8TJ"},
            "expected_goodreads": "53443828",
            "expected_title": "Supers",
            "merge_identify": True,
            "expected_subjects": {"superheroes", "urban fantasy"},
            "required_subjects": {"superheroes", "urban fantasy"},
            "minimum_description": 400,
            "topic": "meteorites",
            "timeout": 30,
        },
        {
            "title": "The Devil's Apprentice",
            "authors": ["Patrick Stewart"],
            "identifiers": {"mobi-asin": "B081S9TTSF"},
            "expected_goodreads": "48908672",
            "expected_title": "The Devil's Apprentice",
            "rejected_isbn": "9788763861595",
            "one_result": True,
            "expected_subjects": {"fantasy", "urban fantasy"},
            "required_subjects": {"fantasy", "urban fantasy"},
            "minimum_description": 400,
            "topic": "london",
            "timeout": 30,
        },
        {
            "title": "Pumpkin Spice & Poltergeist: A cozy sapphic paranormal rom-com (Maple Hollow Book 1)",
            "authors": ["Ali K. Mulford", "K. Elle Morrison"],
            "isbn": "9781923184145",
            "identifiers": {"mobi-asin": "B0D6R3GNS8"},
            "canonical_isbn": "9781923184145",
            "series": ("Maple Hollow", 1.0),
            "expected_subjects": {"romance"},
            "required_subjects": {"romance", "paranormal", "witches"},
            "minimum_description": 300,
            "topic": "jordyn",
            "one_result": True,
            "timeout": 30,
        },
        {
            "title": "Spice Box; Sixteen Steamy Stories",
            "authors": [
                "Raine Miller",
                "Cathryn Fox",
                "Gabrielle Bisset",
                "Erika Wilde",
                "Nina Lane",
                "A.C. James",
                "Kathy Kulig",
                "Stephanie Julian",
                "Geri Foster",
                "Jan Springer",
                "Riley J. Ford",
                "Christina Thacher",
                "Lisa Alder",
                "Sarah Makela",
                "Travis Luedke",
                "Maureen O. Betita",
            ],
            "additional_authors": ["K.M. Scott"],
            "identifiers": {"mobi-asin": "B00J3CEULQ"},
            "canonical_isbn": "9781311437068",
            "expected_goodreads": "22027809",
            "expected_title": "Spice Box: Sixteen Steamy Stories",
            "no_series": True,
            "minimum_description": 300,
            "expected_subjects": {"anthologies", "erotic romance"},
            "required_subjects": {"anthologies", "erotic romance"},
            "topic": "spice box",
            "portrait_cover": True,
            "timeout": 30,
        },
        {
            "title": "Moon Deeds",
            "authors": ["Palmer Pickering"],
            "identifiers": {"mobi-asin": "B07S9PDKTW"},
            "canonical_isbn": "9781732568822",
            "expected_title": "Moon Deeds",
            "series": ("Star Children Saga", 1.0),
            "minimum_description": 300,
            "expected_subjects": {"fantasy", "science fiction", "space opera"},
            "required_subjects": {"fantasy", "science fiction", "space opera"},
            "topic": "cassidy and torr",
            "timeout": 30,
        },
        {
            "title": "Every Layout Relearn CSS layout, by example (2019)",
            "authors": ["Heydon Pickering"],
            "expected_authors": ["Heydon Pickering", "Andy Bell"],
            "expected_title": "Every Layout: Relearn CSS layout, by example",
            "expected_goodreads": "48932917",
            "minimum_description": 300,
            "expected_subjects": {"programming", "web"},
            "topic": "css layout",
            "metadata_only": True,
            "timeout": 30,
        },
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
            "required_covers": ("Goodreads",),
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
            "required_covers": ("Open Library",),
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
            "required_covers": ("Google",),
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
            "required_covers": ("Kindle hi-res covers",),
            "topic": "alternate history",
            "series": ("Tales From Alternate Earths", 2.0),
            "timeout": 30,
        },
        {
            "title": "Dark Town, A Fantasy LitRPG Adventure: Level One of the Dragon's Crawl (Tales of Temerity Book 1)",
            "authors": ["Palmer Pickering"],
            "identifiers": {"mobi-asin": "B0CYWJ48TS"},
            "canonical_isbn": "9781960530028",
            "expected_goodreads": "210280433",
            "minimum_description": 340,
            "expected_subjects": {"fantasy", "litrpg", "cozy fantasy"},
            "topic": "haverly arms",
            "series": ("Tales of Temerity", 1.0),
            "timeout": 30,
        },
        {
            "title": "The Hobbit",
            "authors": ["J. R. R. Tolkien"],
            "isbn": "9780547951973",
            "canonical_isbn": "9780547951973",
            "cover_only": True,
            "required_covers": ("Apple Books covers",),
            # Apple's first result is the ISBN match. Its title search also
            # suggests covers for illustrated and graphic-novel adaptations.
            "isbn_first_covers": ("Apple Books covers",),
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
        if case.get("one_result") and queue.qsize() != 1:
            raise AssertionError(
                f"Expected one edition for {case['title']}, got {queue.qsize()}"
            )
        candidates = list(queue.queue)
        if case.get("merge_identify"):
            for rank, row in enumerate(candidates):
                row.identify_plugin = plugin
                row.relevance_in_source = rank
            candidates = merge_identify_results({plugin: candidates}, ThreadSafeLog())
        catalog = case.get("expected_catalog")
        matching = [
            row
            for row in candidates
            if (
                row.get_identifiers().get(catalog[0]) == catalog[1]
                if catalog
                else (
                    plugin._canonical_isbn(row.get_identifiers().get("isbn"))
                    == case["canonical_isbn"]
                    if "canonical_isbn" in case
                    else row.get_identifiers().get("goodreads")
                    == case["expected_goodreads"]
                )
            )
        ]
        if case.get("cover_only"):
            # Select the exact title when the same ISBN has subtitle variants.
            matching = [
                row
                for row in matching
                if row.title.casefold() == case["title"].casefold()
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
        if (
            case.get("expected_title")
            and book.title.casefold() != case["expected_title"].casefold()
        ):
            raise AssertionError(f"Wrong catalog title: {book.title}")
        if case.get("metadata_only") and (
            book.pubdate or book.get_identifiers().keys() & {"isbn", "asin"}
        ):
            raise AssertionError(f"Unverified edition metadata for {book.title}")
        goodreads = case.get("expected_goodreads")
        if goodreads and book.get_identifiers().get("goodreads") != goodreads:
            raise AssertionError(f"Wrong Goodreads edition for {book.title}")
        rejected_isbn = case.get("rejected_isbn")
        if rejected_isbn and book.get_identifiers().get("isbn") == rejected_isbn:
            raise AssertionError(f"Unverified homonym ISBN for {book.title}")
        if case.get("cover_only"):
            cover_failures.extend(
                check_cover_quality(plugin, cover_sources, book, case, stalled_sources)
            )
            continue
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
        if catalog:
            ids = book.get_identifiers()
            if (
                ids.get("asin") != case["identifiers"]["mobi-asin"]
                or ids.keys() & {"isbn", "goodreads"}
                or (book.series, book.series_index) != case["series"]
                or getattr(book, "has_cached_cover", False)
            ):
                raise AssertionError(
                    f"Incorrect work-only match for {book.title}: {ids}"
                )
            print("LIVE_METADATA_QUALITY_PASS title=" + book.title)
            continue
        description = plugin._usable_description(book.comments)
        if not (description and len(description) >= case["minimum_description"]):
            raise AssertionError("No full live synopsis for " + case["title"])
        if case["topic"] not in description.casefold():
            raise AssertionError("Synopsis has the wrong subject")
        if not book.tags:
            raise AssertionError("Missing final subject tags")
        subjects = {tag.casefold() for tag in book.tags}
        if not case["expected_subjects"].intersection(subjects):
            raise AssertionError(
                f"No relevant subjects for {case['title']}: {book.tags}"
            )
        if case.get("required_subjects", set()) - subjects:
            raise AssertionError(
                f"Missing required subjects for {book.title}: {book.tags}"
            )
        if case.get("exact_tags") and book.tags != case["exact_tags"]:
            raise AssertionError(book.tags)
        if case.get("review_credits"):
            check_review_blocks(description, case["review_credits"])
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
        if case.get("no_series") and book.series:
            raise AssertionError(
                f"Included story's series assigned to {book.title}: {book.series}"
            )
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
        if not case.get("metadata_only"):
            cover_failures.extend(
                check_cover_quality(plugin, cover_sources, book, case, stalled_sources)
            )
    if cover_failures:
        raise AssertionError("\n".join(cover_failures))


if __name__ == "__main__":
    main()
