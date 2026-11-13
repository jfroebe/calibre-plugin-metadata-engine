# -*- coding: utf-8 -*-
"""Integrate Metadata Engine searches and covers with Calibre's Source API."""

from __future__ import annotations

import json
import re
import ssl
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import md5
from html import escape, unescape
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Option, Source


def _log(log, level, message, *values):
    """Format diagnostics before passing them to Calibre's non-logging log API."""
    if values:
        try:
            message = message % values
        except (TypeError, ValueError):
            message = " ".join([str(message), *(str(value) for value in values)])
    getattr(log, level)(message)


class MetadataEngineSource(Source):
    """Provide Calibre metadata and cover lookup through an external engine server.

    Use configured providers or discover enabled providers. Optional bearer
    credentials and TLS preferences apply to HTTP acquisition; successful identify
    results are placed on Calibre's queue rather than written to a library here.
    """

    name = "Metadata Engine"
    description = "Download book metadata and covers from metadata-engine"
    author = "Jason Froebe"
    version = (1, 1, 68)
    minimum_calibre_version = (6, 0, 0)
    supported_platforms = ["windows", "osx", "linux"]

    capabilities = frozenset(["identify", "cover"])
    touched_fields = frozenset(
        [
            "title",
            "authors",
            "publisher",
            "pubdate",
            "comments",
            "languages",
            "tags",
            "series",
            "series_index",
            "identifier:isbn",
            "identifier:asin",
            "identifier:goodreads",
            "identifier:lccn",
            "identifier:oclc",
            "identifier:dewey",
            "identifier:openlibrary",
            "identifier:metadata_engine_media",
        ]
    )
    has_html_comments = True
    supports_gzip_transfer_encoding = True
    cached_cover_url_is_reliable = False
    can_get_multiple_covers = True
    # Calibre otherwise discards ISBN-less matches whenever this source also
    # returns an ISBN-bearing result, even if that result is a different book.
    prefer_results_with_isbn = False

    options = (
        Option(
            "base_url",
            "string",
            "http://127.0.0.1:8790",
            "Metadata Engine URL",
            "Base URL of metadata-engine.",
        ),
        Option(
            "plugin_ids",
            "string",
            "",
            "Metadata Engine plugin IDs",
            "All enabled book providers are used. These IDs are a fallback if discovery is unavailable.",
        ),
        Option(
            "max_results",
            "number",
            20,
            "Maximum results",
            "Maximum metadata results returned to Calibre.",
        ),
        Option(
            "per_plugin_limit",
            "number",
            10,
            "Per-provider result limit",
            "Maximum results requested from each metadata-engine provider.",
        ),
        Option(
            "api_token", "string", "", "Bearer token", "Optional HTTP Bearer token."
        ),
        Option(
            "verify_ssl",
            "bool",
            True,
            "Verify HTTPS certificates",
            "Verify TLS certificates for HTTPS connections.",
        ),
    )

    def __init__(self, *args, **kwargs):
        """Initialize the Calibre source base and a per-instance identifier-to-cover cache."""
        super().__init__(*args, **kwargs)
        self._cover_urls = {}

    def is_configured(self):
        """Return whether the resolved engine base URL is nonempty."""
        return bool(self._base_url())

    def config_widget(self):
        """Create the Qt settings editor for this plugin instance."""
        from calibre_plugins.metadata_engine.config import ConfigWidget

        return ConfigWidget(self)

    def save_settings(self, config_widget):
        """Validate and commit settings through the supplied configuration widget."""
        config_widget.commit()

    def identify(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers=None,
        timeout=30,
    ):
        """Queue normalized book metadata from configured or discovered engine providers.

        Honor abort between provider/result operations and isolate provider failures.
        Return None on completion/cancellation, or an explanatory error string when
        query construction or the outer operation fails. Individual provider failures
        are logged and do not suppress other successful candidates.
        """
        identifiers = identifiers or {}
        if authors:
            authors = [self._canonical_author_name(author) for author in authors]
        query = self._make_query(title, authors, identifiers)
        if not query:
            return (
                "Metadata Engine requires a title, author, ISBN, ASIN, or identifier."
            )

        try:
            provider_ids = self._book_provider_ids(log, timeout, abort)
            initial = self._initial_provider_batch(
                provider_ids, query, authors, identifiers, timeout, abort
            )
            rows = []
            retried_providers = set()
            for provider_id in provider_ids:
                media_types = (None, "books") if provider_id == "itunes" else (None,)
                for media_type in media_types:
                    if abort.is_set():
                        return None
                    try:
                        found, error = initial[(provider_id, media_type)]
                        if error is not None:
                            raise error
                        if provider_id == "goodreads" and not found and title:
                            short_title = self._search_title(title)
                            if short_title != query.strip():
                                _log(
                                    log,
                                    "info",
                                    "Retrying Goodreads with a shortened title",
                                )
                                found = self._search(
                                    provider_id, short_title, None, {}, timeout
                                )
                                retried_providers.add(provider_id)
                        if (
                            not found
                            and title
                            and len(self._list(authors)) > 1
                            and provider_id
                            in {
                                "goodreads",
                                "google_books",
                                "hardcover",
                                "open_library",
                                "itunes",
                            }
                            # Once a catalog has identified the edition, use its
                            # ISBN in the next tier instead of broad author retries.
                            and not any(
                                self._canonical_isbn(row.get("isbn"))
                                and self._matches_requested(
                                    row, title, authors, identifiers
                                )
                                for row in rows
                            )
                        ):
                            short_title = self._search_title(title)
                            variants = [None, *self._list(authors)[:3]]
                            for person in variants:
                                if abort.is_set():
                                    return None
                                if person is None and provider_id in retried_providers:
                                    continue
                                retry = self._search(
                                    provider_id,
                                    short_title,
                                    [person] if person else None,
                                    {},
                                    timeout,
                                    **(
                                        {"metadata_type": media_type}
                                        if media_type
                                        else {}
                                    ),
                                )
                                if person is None:
                                    retried_providers.add(provider_id)
                                target = {"title": title, "authors": authors}
                                matching = [
                                    item
                                    for item in retry
                                    if self._work_description_matches(target, item)
                                ]
                                _log(
                                    log,
                                    "info",
                                    "Metadata Engine: contributor search provider=%s candidates=%d matches=%d",
                                    provider_id,
                                    len(retry),
                                    len(matching),
                                )
                                if matching:
                                    found.extend(matching)
                                    break
                        if provider_id == "google_books":
                            matches = sum(
                                self._matches_requested(
                                    row, title, authors, identifiers
                                )
                                for row in found
                            )
                            _log(
                                log,
                                "info",
                                "Metadata Engine Google Books: candidates=%d matching=%d",
                                len(found),
                                matches,
                            )
                        rows.extend(found)
                    except Exception as exc:
                        _log(
                            log,
                            "error",
                            "Metadata Engine provider %r failed: %s",
                            provider_id,
                            exc,
                        )

            if (
                self._catalog_index_title(self._text(title))
                and not abort.is_set()
                and not any(
                    self._matches_requested(row, title, authors, identifiers)
                    for row in rows
                )
            ):
                # A title with this punctuation can also be literal. Retain
                # the original spelling when the shorter search has no match.
                catalogs = [
                    provider
                    for provider in provider_ids
                    if provider
                    in {"google_books", "goodreads", "hardcover", "open_library"}
                ]
                literal = self._initial_provider_batch(
                    catalogs, title, authors, identifiers, min(timeout, 5), abort
                )
                for found, error in literal.values():
                    if error is None:
                        rows.extend(found)

            authors = self._recover_incomplete_author(
                log, rows, title, authors, identifiers
            )

            # An exact edition identifier ties the provider result to the
            # existing Calibre record even when the provider omits its authors.
            if authors:
                for row in rows:
                    for kind, aliases in (
                        ("asin", ("asin",)),
                        ("isbn", ("isbn", "isbn13", "isbn10")),
                    ):
                        wanted = self._identifier(identifiers, *aliases)
                        actual = self._text(row.get(kind))
                        if wanted and actual and not self._list(row.get("authors")):
                            wanted = re.sub(r"[\s-]+", "", wanted).upper()
                            actual = re.sub(r"[\s-]+", "", actual).upper()
                            if wanted == actual and self._matches_requested(
                                row, title, authors, identifiers
                            ):
                                row["authors"] = self._list(authors)
                                break

            # A title-only lookup can return an authorless edition. Trust the
            # requested contributors only when a second source independently
            # confirms the same work and contributor set.
            target = {"title": title, "authors": authors}
            if title and authors:
                verified = [
                    row for row in rows if self._work_description_matches(target, row)
                ]
                for row in rows:
                    if self._list(row.get("authors")):
                        continue
                    for witness in verified:
                        if (
                            row.get("source")
                            and witness.get("source")
                            and row.get("source") != witness.get("source")
                            and self._identity_consistent(target, row)
                            and self._identity_consistent(row, witness)
                            and self._compatible_identifiers(row, witness)
                        ):
                            row["authors"] = self._list(authors)
                            _log(
                                log,
                                "info",
                                "Metadata Engine: corroborated missing contributors using provider=%s",
                                witness.get("source"),
                            )
                            break

            goodreads_editions = {}
            rows = self._expand_identifiers(
                log,
                abort,
                provider_ids,
                rows,
                query,
                title,
                authors,
                identifiers,
                timeout,
                goodreads_editions,
            )
            series, position = self._series_identity({"title": title})
            if series and position is not None:
                series_target = {"series": [{"name": series, "index": position}]}
                rows = [
                    row for row in rows if self._identity_consistent(series_target, row)
                ]
            rows = self._recover_descriptions(
                log,
                abort,
                provider_ids,
                rows,
                query,
                title,
                authors,
                identifiers,
                timeout,
                retried_providers,
            )
            rows = [
                self._with_requested_series(row, title, authors, identifiers)
                for row in rows
            ]
            rows = self._recover_supplements(
                log,
                abort,
                provider_ids,
                rows,
                title,
                authors,
                identifiers,
                timeout,
                retried_providers,
            )
            if "goodreads" in provider_ids:
                self._recover_catalog_series(
                    log, rows, title, authors, identifiers, abort, timeout
                )
            requested = {
                "title": title,
                "authors": authors,
                "isbn": self._identifier(identifiers, "isbn", "isbn13", "isbn10"),
                "asin": self._cover_asin(identifiers),
            }
            indexed = self._catalog_index_title(self._text(title))
            if indexed and self._canonical_isbn(requested["isbn"]):
                # Normalize verified spellings before deduplication. An
                # alternate title can still describe the very same ISBN.
                rows = [
                    dict(row, title=indexed[1], subtitle=None)
                    if self._indexed_title_matches(requested, row, *indexed)
                    and self._matches_requested(row, title, authors, identifiers)
                    else row
                    for row in rows
                ]
            rows = self._dedupe(rows)
            # A shared title alone cannot verify an authorless candidate.
            # Keep rows corroborated above or tied to a requested identifier.
            compatible = [
                row
                for row in rows
                if self._identity_consistent(requested, row)
                and (
                    not authors
                    or self._list(row.get("authors"))
                    or self._matches_requested(row, title, authors, identifiers)
                )
            ]
            rejected = len(rows) - len(compatible)
            if rejected:
                _log(
                    log,
                    "info",
                    f"Metadata Engine rejected {rejected} conflicting title/author results",
                )
            rows = compatible
            asin = self._cover_asin(identifiers)
            sorted_kindle = (
                re.fullmatch(r"B[A-Z0-9]{9}", asin)
                and self._restore_title_article(self._text(title) or "") != title
            )
            if (
                "goodreads" in provider_ids
                and (not identifiers or re.fullmatch(r"B[A-Z0-9]{9}", asin))
                and (
                    not rows
                    or (
                        sorted_kindle
                        and not any(self._cover_asin(row) == asin for row in rows)
                    )
                )
            ):
                try:
                    recovered = self._goodreads_work_fallback(
                        title, authors, abort, timeout, identifiers
                    )
                    if recovered and recovered[0].get("verified_original_title"):
                        title = requested["title"] = recovered[0]["title"]
                    rows = recovered or rows
                except (OSError, RuntimeError, ValueError) as exc:
                    _log(
                        log,
                        "warning",
                        "Metadata Engine Goodreads title fallback failed: %s",
                        exc,
                    )

            isbn = self._canonical_isbn(
                self._identifier(identifiers, "isbn", "isbn13", "isbn10")
            )
            if not any(
                self._matches_requested(row, title, authors, identifiers)
                and (
                    not self._is_print_edition(row)
                    or not (isbn or asin)
                    or (isbn and isbn == self._canonical_isbn(row.get("isbn")))
                    or (asin and asin == self._cover_asin(row))
                )
                for row in rows
            ):
                print_match = self._recover_print_metadata(
                    log, abort, provider_ids, rows, requested, timeout
                )
                if print_match:
                    rows = [print_match]

            if not rows and re.fullmatch(r"B[A-Z0-9]{9}", asin):
                work = self._recover_omnibus(log, requested, abort, timeout)
                if work:
                    rows = [work]

            exact = [
                row
                for row in rows
                if (
                    (isbn and isbn == self._canonical_isbn(row.get("isbn")))
                    or (asin and asin == self._cover_asin(row))
                )
                and self._matches_requested(row, title, authors, identifiers)
            ]
            if exact:
                for winner in exact:
                    # Require numbered series evidence before borrowing subjects
                    # across explicitly different ISBNs.
                    series, position = self._series_identity(winner)
                    if not series or position is None:
                        continue
                    for donor in rows:
                        if donor not in exact and (
                            self._work_description_matches(winner, donor)
                            or (
                                self._work_description_matches(requested, winner)
                                and self._work_description_matches(requested, donor)
                            )
                        ):
                            for field in ("genres", "tags"):
                                values = self._list(winner.get(field)) + self._list(
                                    donor.get(field)
                                )
                                if values:
                                    winner[field] = list(dict.fromkeys(values))
                rows = exact
            target = requested

            def rank(row):
                confirmed = bool(
                    isbn
                    and isbn == self._canonical_isbn(row.get("isbn"))
                    and self._work_description_matches(target, row)
                )
                has_synopsis = bool(self._usable_description(row.get("description")))
                confidence = float(row.get("confidence") or 0)
                return confirmed, confirmed and has_synopsis, confidence

            rows.sort(key=rank, reverse=True)
            if indexed and rows and isbn and rank(rows[0])[0]:
                # One indexed title and exact ISBN identify a single requested
                # edition. Prefer the richest compatible record without
                # mixing source records that disagree on secondary IDs.
                rows = rows[:1]
            if rows and isbn and rank(rows[0])[0] and not abort.is_set():
                self._recover_book_subjects(
                    log, rows[0], isbn, timeout, provider_ids, abort
                )
                if "goodreads" in provider_ids and not abort.is_set():
                    self._recover_goodreads_isbn_series(
                        log, rows[0], isbn, timeout, requested
                    )
            maximum = max(1, int(self.prefs.get("max_results", 20) or 20))
            requested_asin = self._cover_asin(identifiers)
            if (
                rows
                and "goodreads" in provider_ids
                and re.fullmatch(r"B[A-Z0-9]{9}", requested_asin)
                and not abort.is_set()
            ):
                candidate = next(
                    (
                        row
                        for row in rows[:maximum]
                        if self._matches_requested(row, title, authors, identifiers)
                    ),
                    None,
                )
                tags = self._to_metadata(candidate, 0).tags if candidate else []
                if candidate is not None and not self._has_specific_subjects(tags):
                    self._recover_goodreads_subjects(
                        log,
                        candidate,
                        requested_asin,
                        abort,
                        timeout,
                        goodreads_editions,
                        discover=bool(tags),
                    )

            for relevance, row in enumerate(rows[:maximum]):
                if abort.is_set():
                    return None
                matches_request = self._matches_requested(
                    row, title, authors, identifiers
                )
                if matches_request and self._work_description_matches(requested, row):
                    row = self._with_catalog_subtitle_genre(row)
                mi = self._to_metadata(self._with_edition_correction(row), relevance)
                if mi is not None:
                    mi.authors = self._description_authors(mi.comments, mi.authors)
                    if matches_request:
                        # Calibre passes the result's identifiers to its cover
                        # workers. Catalog records may omit the saved Kindle ID.
                        if not row.get("asin") and re.fullmatch(
                            r"B[A-Z0-9]{9}", requested_asin
                        ):
                            mi.set_identifier("asin", requested_asin)
                        missing = []
                        if not self._usable_description(mi.comments):
                            missing.append("description")
                        if not mi.tags:
                            missing.append("tags")
                        if self._warning_referral(mi.comments):
                            missing.append("content warning details")
                        if missing:
                            _log(
                                log,
                                "warning",
                                "Metadata Engine incomplete matching result: "
                                + ", ".join(missing)
                                + f"; source={row.get('source')}",
                            )
                    self.clean_downloaded_metadata(mi)
                    result_queue.put(mi)
            return None
        except Exception as exc:
            _log(log, "exception", "Metadata Engine identify failed")
            return "Metadata Engine identify failed: %s" % exc

    @classmethod
    def _is_print_edition(cls, row):
        """Require an explicit paperback or hardcover binding from the catalog."""
        extra = row.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        binding = " ".join(
            cls._text(record.get(key)) or ""
            for record in (row, extra)
            for key in ("format", "physical_format", "edition_format", "binding")
        )
        media = cls._text(row.get("metadata_type")) or ""
        return bool(
            re.search(r"\b(?:paperback|softcover|hardcover|hardback)\b", binding, re.I)
            and not re.search(
                r"\b(?:audio(?:book)?s?|e-?books?|kindle|comics?|graphic novels?|manga)\b",
                f"{binding} {media}",
                re.I,
            )
        )

    def _recover_print_metadata(self, log, abort, provider_ids, rows, target, timeout):
        """Retry by title and author and keep only verified work metadata."""
        authors = self._list(target.get("authors"))
        query = self._search_title(target.get("title"))
        if not query or not authors or abort.is_set():
            return None

        def matching_metadata(candidates):
            for row in candidates:
                if not self._is_print_edition(row) or len(
                    self._list(row.get("authors"))
                ) != len(authors):
                    continue
                variants = [row]
                title = self._text(row.get("title"))
                subtitle = self._text(row.get("subtitle"))
                if title and not subtitle:
                    parenthetical = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", title)
                    if parenthetical:
                        title, subtitle = parenthetical.groups()
                if title and subtitle:
                    # Print catalogs sometimes move a franchise heading after
                    # the work title. Require both labels to match in full.
                    variants.append(
                        dict(row, title=f"{subtitle}: {title}", subtitle=None)
                    )
                if not any(
                    self._identity_consistent(target, variant)
                    and self._work_description_matches(target, variant)
                    for variant in variants
                ):
                    continue
                row = self._with_edition_correction(row)
                metadata = {
                    field: row[field]
                    for field in (
                        "series",
                        "genres",
                        "tags",
                        "content_warnings",
                        "trigger_warnings",
                        "parental_warnings",
                    )
                    if row.get(field)
                }
                description = self._usable_description(row.get("description"))
                if description:
                    metadata["description"] = description
                if metadata:
                    # Calibre already has the user's edition. Copy only fields
                    # that describe the work and retain the requested title/IDs.
                    yield dict(target, **metadata, source=row.get("source"))

        matches = list(matching_metadata(rows))
        if not matches:
            providers = [
                provider
                for provider in provider_ids
                if provider
                in {"google_books", "goodreads", "hardcover", "open_library"}
            ]
            batch = self._initial_provider_batch(
                providers, query, authors, {}, min(timeout, 5), abort
            )
            for (provider, _), (donors, error) in batch.items():
                if error is not None:
                    _log(
                        log,
                        "info",
                        "Metadata Engine print lookup provider=%s failed: %s",
                        provider,
                        error,
                    )
                    continue
                matches.extend(matching_metadata(donors))
        if not matches or abort.is_set():
            return None
        # Prefer a usable synopsis, then merge subjects and series only when
        # the independently verified print records are compatible.
        matches.sort(key=lambda row: bool(row.get("description")), reverse=True)
        result = matches[0]
        for donor in matches[1:]:
            self._merge_metadata(result, donor)
        _log(
            log, "info", "Metadata Engine recovered work metadata from a print edition"
        )
        return result

    def _recover_omnibus(self, log, target, abort, timeout):
        """Recover work fields from an independently cataloged complete collection."""
        from lxml.html import fromstring

        title = self._text(target.get("title")) or ""
        match = re.fullmatch(
            r"(.+?)\s+\((?:complete story|complete series|omnibus|box(?:ed)? set)\)"
            r"(?:\s*:\s*.+)?",
            title,
            re.IGNORECASE,
        )
        authors = self._list(target.get("authors"))
        if not match or len(authors) != 1 or abort.is_set():
            return None
        base = match[1].strip()
        author = self._author_words(authors[0])
        if len(author) < 2 or not all(re.fullmatch(r"[a-z]+", word) for word in author):
            return None
        slug = "-".join(re.findall(r"[a-z0-9]+", base.casefold().replace("'", "")))
        if not slug:
            return None
        url = f"https://www.fantasticfiction.com/{author[-1][0]}/{'-'.join(author)}/{slug}.htm"
        try:
            payload = self._http_bytes(url, min(timeout, 6))
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                log,
                "info",
                "Metadata Engine omnibus lookup failed: %s",
                str(exc).split("\n")[0][:200],
            )
            return None if abort.is_set() else self._cataloged_omnibus(target)
        if abort.is_set() or not payload.strip():
            return None
        document = fromstring(payload)
        headings = document.xpath("//div[@class='bookheading']")
        blurbs = document.xpath("//div[@class='blurb']")
        if len(headings) != 1 or len(blurbs) != 1:
            return None
        heading, blurb = headings[0], blurbs[0]
        found_title = heading.xpath("normalize-space(./h1[@itemprop='name'])")
        people = heading.xpath(".//*[@itemprop='author']//*[@itemprop='name']")
        if (
            self._work_title(found_title)[0] != self._work_title(base)[0]
            or len(people) != 1
            or self._author_words(people[0].text_content()) != author
            or not re.search(
                r"\b(?:omnibus|box(?:ed)? set)\b", heading.text_content(), re.I
            )
        ):
            return None
        genres = [
            node.text_content().strip()
            for node in blurb.xpath(".//a[starts-with(@href, '/genres/')]")
        ]
        # Preserve paragraph breaks as text and escape it before handing it to Calibre.
        for node in blurb.xpath(".//br | .//li | .//h4 | .//p"):
            node.tail = "\n" + (node.tail or "")
        for node in blurb.xpath(".//script | .//style"):
            node.drop_tree()
        synopsis = re.split(r"\s*Genre:\s*", blurb.text_content(), maxsplit=1)[0]
        paragraphs = [
            " ".join(line.split()) for line in synopsis.splitlines() if line.strip()
        ]
        if not paragraphs or not any(genres):
            return None
        series = heading.xpath("normalize-space(./a[1])")
        _log(
            log,
            "info",
            "Metadata Engine recovered omnibus metadata from Fantastic Fiction",
        )
        # The catalog confirms the work. The ASIN comes from the Calibre request.
        return {
            "title": found_title,
            "authors": authors,
            "asin": target["asin"],
            "description": "".join(f"<p>{escape(line)}</p>" for line in paragraphs),
            "genres": [*filter(None, genres), "Omnibus"],
            "series": [{"name": series, "position": None}] if series else [],
            "source": "fantastic_fiction",
        }

    @classmethod
    def _cataloged_omnibus(cls, target):
        """Supply work fields for a corroborated Kindle omnibus when catalogs fail."""
        # StoryGraph confirms the Kindle ASIN; Fantastic Fiction confirms the
        # omnibus contents, synopsis, genre, and unnumbered collection label.
        # https://app.thestorygraph.com/books/c94518ac-31df-4a98-96b1-909eec673d0d
        # https://www.fantasticfiction.com/f/darcy-fayton/collared-and-groomed-by-the-vampire-prince.htm
        title = "Collared and Groomed by the Vampire Prince"
        work = {
            "title": title,
            "authors": ["Darcy Fayton"],
            "asin": "B0DNWMQBN8",
            "series": [{"name": "Collared Duet", "position": None}],
            "genres": ["Paranormal Romance", "Omnibus"],
            "description": (
                "<p>At Volmasque Academy, Kira plans to unite the wolf shifters "
                "against the vampires who control them. Nathaniel, the prince "
                "who holds her captive, has plans of his own. As their attraction "
                "grows, Kira and Nathaniel must decide how far they will go for "
                "revenge and what they are willing to risk for each other.</p>"
                "<p>This omnibus contains Collared by the Vampire Prince, "
                "Groomed by the Vampire Prince, and the bonus chapter "
                "Plugged at the Gates.</p>"
            ),
            "source": "fantastic_fiction",
        }
        requested_title = cls._work_title(target.get("title"))[0]
        base = title.casefold()
        if (
            target.get("asin") == work["asin"]
            and (
                requested_title == base
                or requested_title.startswith(base + " (complete story): ")
            )
            and cls._work_description_matches(dict(target, title=title), work)
        ):
            return work
        return None

    @classmethod
    def _with_catalog_subtitle_genre(cls, row):
        """Use explicit genre wording in the matched catalog's fiction subtitle."""
        genres = cls._list(row.get("genres"))
        subjects = {tag.casefold() for tag in genres + cls._list(row.get("tags"))}
        if "fiction" not in subjects or "horror" in subjects:
            return row
        subtitle = cls._text(row.get("subtitle")) or row["title"].partition(":")[2]
        if re.search(r"\bslasher\s+horror\s*[.!]?\s*$", subtitle, re.IGNORECASE):
            return dict(row, genres=[*genres, "Horror"])
        return row

    @staticmethod
    def _has_specific_subjects(tags):
        """Treat generic catalog categories as insufficient for genre recovery."""
        return any(
            tag.casefold() not in {"fiction", "nonfiction", "non-fiction"}
            for tag in tags
        )

    def _recover_book_subjects(self, log, row, isbn, timeout, provider_ids, abort):
        """Enrich the selected ISBN without replacing its edition fields."""
        if self._has_specific_subjects(self._to_metadata(row, 0).tags):
            return
        deadline = time.monotonic() + min(timeout, 5)
        authors = self._list(row.get("authors"))
        open_library_enabled = "open_library" in provider_ids
        if "tertulia_subjects" in provider_ids:
            try:
                donors = self._search(
                    "tertulia_subjects",
                    row["title"],
                    authors,
                    {"isbn": isbn},
                    min(timeout, 2 if open_library_enabled else 5),
                )
                for donor in donors:
                    if self._same_edition(row, donor):
                        row["tags"] = self._list(row.get("tags")) + self._list(
                            donor.get("tags")
                        )
            except (OSError, RuntimeError, ValueError) as exc:
                _log(
                    log,
                    "info",
                    "Metadata Engine Tertulia subject lookup failed: %s",
                    exc,
                )
        remaining = deadline - time.monotonic()
        if (
            not open_library_enabled
            or remaining <= 0
            or abort.is_set()
            or self._has_specific_subjects(self._to_metadata(row, 0).tags)
        ):
            return
        try:
            genres = self._open_library_isbn_genres(row, isbn, remaining)
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                log, "info", "Metadata Engine Open Library ISBN lookup failed: %s", exc
            )
            return
        if genres:
            row["genres"] = self._list(row.get("genres")) + genres
            _log(
                log,
                "info",
                "Metadata Engine recovered genres from Open Library ISBN %s",
                isbn,
            )

    def _recover_goodreads_isbn_series(self, log, row, isbn, timeout, requested=None):
        """Fill missing series information using the verified Goodreads edition."""
        current_name, current_position = self._series_identity(row)
        if current_position is not None:
            return
        deadline = time.monotonic() + min(timeout, 20)
        try:
            _, url = self._http_response(
                f"https://www.goodreads.com/book/isbn/{isbn}", min(timeout, 14)
            )
            resolved = urlsplit(url)
            book_id = re.fullmatch(
                r"/(?:[a-z]{2}/)?book/show/([0-9]{1,16})(?:[-.][^/]*)?/?",
                resolved.path,
            )
            remaining = deadline - time.monotonic()
            if (
                resolved.scheme != "https"
                or resolved.hostname != "www.goodreads.com"
                or not book_id
                or remaining <= 0
            ):
                return
            donor = self._goodreads_book(book_id[1], remaining)
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                log,
                "info",
                "Metadata Engine Goodreads ISBN series lookup failed: %s",
                exc,
            )
            return
        if not donor or self._canonical_isbn(donor.get("isbn")) != isbn:
            return
        work = dict(donor, series=None)
        titles = [work]
        parenthetical = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", donor["title"])
        if parenthetical:
            title, label = parenthetical.groups()
            titles.append(dict(work, title=f"{label}: {title}"))
        indexed = self._catalog_index_title(self._text((requested or {}).get("title")))
        witnesses = [row, requested] if indexed else [row]
        if not any(
            self._work_description_matches(witness, variant)
            for witness in witnesses
            for variant in titles
        ):
            return

        numbered = {}
        collections = {}
        for item in donor.get("series") or []:
            name, position = self._series_identity({"series": [item]})
            key = self._series_name_key(name)
            label = self._text(item.get("position"))
            same_franchise = indexed and (
                self._franchise_label_matches(indexed[0], current_name)
                and self._franchise_label_matches(indexed[0], name)
            )
            if not name or (
                current_name
                and key != self._series_name_key(current_name)
                and not same_franchise
            ):
                continue
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", label or ""):
                numbered[(key, position)] = item
            elif (
                not label
                and indexed
                and self._franchise_label_matches(indexed[0], name)
            ):
                collections[key] = item
        series = numbered or collections
        if len(series) == 1:
            row["series"] = list(series.values())
            _log(
                log,
                "info",
                "Metadata Engine recovered series from Goodreads ISBN %s",
                isbn,
            )

    def _open_library_isbn_genres(self, row, isbn, timeout):
        """Read fiction subject classifications from an independently verified ISBN."""
        key = f"ISBN:{isbn}"
        params = urlencode({"bibkeys": key, "jscmd": "data", "format": "json"})
        data = self._http_json(f"https://openlibrary.org/api/books?{params}", timeout)
        record = data.get(key)
        if not isinstance(record, dict):
            return []
        identifiers = record.get("identifiers")
        if not isinstance(identifiers, dict) or not any(
            self._canonical_isbn(value) == isbn
            for field in ("isbn_13", "isbn_10")
            for value in self._list(identifiers.get(field))
        ):
            return []
        title = self._text(record.get("title"))
        subtitle = self._text(record.get("subtitle"))
        people = record.get("authors")
        subjects = record.get("subjects")
        if not isinstance(people, list) or not isinstance(subjects, list):
            return []
        authors = [
            person["name"]
            for person in people
            if isinstance(person, dict) and self._text(person.get("name"))
        ]
        donor = {"title": title, "subtitle": subtitle, "authors": authors, "isbn": isbn}
        # The catalog can put a franchise heading after the work title.
        # Allow the reversal only when both labeled parts and the ISBN agree.
        titles = [donor]
        if title and subtitle:
            titles.append(dict(donor, title=f"{subtitle}: {title}", subtitle=None))
        if not any(
            self._work_description_matches(row, variant)
            and self._identity_consistent(row, variant)
            for variant in titles
        ):
            return []
        genres = []
        # Open Library can mix work-wide topics, including related comics, into
        # this response. Import only the genre hierarchy under Fiction.
        for subject in subjects:
            if not isinstance(subject, dict):
                continue
            parts = self._list((self._text(subject.get("name")) or "").split(","))
            if len(parts) < 2 or parts[0].casefold() != "fiction":
                continue
            genres.extend(
                part.title() if part.islower() else part
                for part in parts[1:]
                if part.casefold() != "general"
            )
        return genres

    def _recover_supplements(
        self,
        log,
        abort,
        provider_ids,
        rows,
        title,
        authors,
        identifiers,
        timeout,
        retried_providers=None,
    ):
        """Retry missing subjects or referred warning details through enabled sources."""
        # Preserve corroborating edition/series variants as bridges before
        # deduplication chooses a single display title with a catalog subtitle.
        for winner in rows:
            for donor in rows:
                if winner is not donor:
                    self._merge_metadata(winner, donor)
        matches = [
            row
            for row in self._dedupe(rows)
            if self._matches_requested(row, title, authors, identifiers)
        ]
        if not matches:
            return rows
        candidate = matches[0]
        needs_tags = not self._to_metadata(candidate, 0).tags
        needs_warnings = self._warning_referral(candidate.get("description")) and not (
            self._list(candidate.get("trigger_warnings"))
            or self._list(candidate.get("content_warnings"))
        )
        if not needs_tags and not needs_warnings:
            return rows
        query = self._search_title(candidate.get("title"), candidate.get("subtitle"))
        retry_authors = self._list(candidate.get("authors"))
        retry_ids = {}
        providers = [
            p
            for p in provider_ids
            if p not in {"isbnlab", "asintojson"}
            and (needs_warnings or p not in (retried_providers or set()))
        ]
        isbn = self._canonical_isbn(candidate.get("isbn"))
        volume = self._numbered_volume(candidate)
        if needs_tags and not needs_warnings and isbn and volume:
            # The subtitle describes the volume. Catalogs index the numbered
            # heading and may disagree on the complete anthology author list.
            query = f"{volume[0]} {volume[1]:g}"
            retry_authors = None
            retry_ids = {"isbn": isbn}
            providers = [
                p
                for p in provider_ids
                if p in {"goodreads", "google_books", "hardcover", "open_library"}
            ]
        batch = self._initial_provider_batch(
            providers,
            query,
            retry_authors,
            retry_ids,
            min(timeout, 5),
            abort,
        )
        for provider in providers:
            if abort.is_set():
                return rows
            for mode in (None, "books") if provider == "itunes" else (None,):
                donors, error = batch[(provider, mode)]
                if error is not None:
                    _log(
                        log,
                        "info",
                        "Metadata Engine supplement retry %s failed: %s",
                        provider,
                        error,
                    )
                    continue
                for winner in rows:
                    if not self._matches_requested(winner, title, authors, identifiers):
                        continue
                    for donor in donors:
                        merged = self._merge_metadata(winner, donor)
                        if not merged and retry_ids and needs_tags:
                            self._merge_anthology_subjects(winner, donor)
        return rows

    @staticmethod
    def _warning_referral(value):
        """Recognize a referral sentence, which contains no actual warning labels."""
        return re.search(
            r"(?:trigger\s+and\s+content|trigger|content)\s+warnings\s+"
            r"(?:are\s+)?available\s+(?:on|in)\s+[^.!?]+[.!?]?",
            value or "",
            re.IGNORECASE,
        )

    @staticmethod
    def _canonical_author_name(name):
        """Expand explicit catalog given names and remove authority dates."""
        value = re.sub(r"^\d{4}(?:-\d{0,4})?\s+", "", name.strip())
        value = re.sub(r"(?:,\s*|\s+)\d{4}(?:-\d{0,4})?$", "", value).strip()
        expanded = re.search(r"\(([^()]+)\)", value)
        if expanded:
            before = value[: expanded.start()].strip()
            given = expanded[1].strip()
            words = before.replace(",", " ").split()
            if given and len(words) >= 2:
                first = given.split()[0].casefold()
                if "," in before and words[1].casefold() == first:
                    return given + " " + before.split(",", 1)[0].strip()
                if words[1].casefold() == first:
                    return given + " " + words[0]
                if words[0].casefold() == first:
                    return given + " " + words[-1]
        if "," in value:
            surname, given = value.split(",", 1)
            if surname.strip() and given.strip():
                return given.strip() + " " + surname.strip()
        return value

    @classmethod
    def _author_words(cls, name):
        """Return comparable words without discarding author identity."""
        return tuple(
            re.findall(r"[^\W\d_]+", cls._canonical_author_name(name).casefold())
        )

    @staticmethod
    def _author_words_match(left, right):
        """Allow omitted middle names or initials, retaining first and last names."""
        if left == right:
            return True
        if min(len(left), len(right)) < 2:
            return False
        if left[0] != right[0] or left[-1] != right[-1]:
            return False
        a, b = left[1:-1], right[1:-1]
        if not a or not b:
            return True
        return len(a) == len(b) and all(
            x == y
            or (len(x) == 1 and y.startswith(x))
            or (len(y) == 1 and x.startswith(y))
            for x, y in zip(a, b)
        )

    def _recover_incomplete_author(self, log, rows, title, authors, identifiers):
        """Recover a truncated single author from corroborated edition evidence."""
        people = self._list(authors)
        isbn = self._canonical_isbn(
            self._identifier(identifiers, "isbn", "isbn13", "isbn10")
        )
        if len(people) != 1 or not isbn:
            return authors
        wanted = self._author_words(people[0])
        if len(wanted) < 2:
            return authors
        target = {"title": title, "isbn": isbn}
        candidates = [
            row
            for row in rows
            if self._canonical_isbn(row.get("isbn")) == isbn
            and self._work_titles_compatible(target, row)
            and len(self._list(row.get("authors"))) == 1
        ]
        for row in candidates:
            person = self._list(row.get("authors"))[0]
            full = self._author_words(person)
            if len(full) <= len(wanted) or full[: len(wanted)] != wanted:
                continue
            if not row.get("source"):
                continue
            witnesses = [
                other
                for other in candidates
                if other.get("source")
                and other["source"] != row["source"]
                and self._work_description_matches(row, other)
            ]
            if witnesses:
                corrected = self._canonical_author_name(person)
                _log(
                    log,
                    "info",
                    f"Metadata Engine recovered incomplete author {people[0]!r} "
                    f"as {corrected!r} using corroborated ISBN {isbn}",
                )
                return [corrected]
        return authors

    def _matches_requested(self, row, title, authors, identifiers):
        """Reject ID matches that contradict the requested title or contributors."""
        target = {
            "title": title,
            "authors": authors,
            "isbn": self._identifier(identifiers, "isbn", "isbn13", "isbn10"),
            "asin": self._cover_asin(identifiers),
        }
        if not self._identity_consistent(target, row):
            return False
        matched_identifier = bool(
            target["asin"] and self._cover_asin(row) == target["asin"]
        )
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        for key, aliases in (
            ("asin", ("asin",)),
            ("isbn", ("isbn", "isbn13", "isbn10")),
            ("lccn", ("lccn",)),
            ("oclc", ("oclc",)),
            ("goodreads", ("goodreads",)),
            ("openlibrary", ("openlibrary",)),
        ):
            wanted = self._identifier(identifiers, *aliases)
            if key in {"lccn", "oclc", "goodreads", "openlibrary"}:
                extra_key = {
                    "goodreads": "goodreads_id",
                    "openlibrary": "open_library_edition_id",
                    "lccn": "lccn",
                    "oclc": "oclc",
                }[key]
                candidates = self._list(extra.get(extra_key))
                if key == "oclc":
                    candidates += self._list(extra.get("oclc_numbers"))
                normalized_requested = re.sub(r"[\s-]+", "", wanted or "").upper()
                actual = (
                    next(
                        (
                            item
                            for item in candidates
                            if re.sub(r"[\s-]+", "", item).upper()
                            == normalized_requested
                        ),
                        None,
                    )
                    if wanted
                    else None
                )
                if not actual and candidates:
                    actual = candidates[0]
                if key == "openlibrary" and wanted and wanted.upper().endswith("W"):
                    actual = self._text(extra.get("open_library_work_id"))
                elif key == "openlibrary" and not actual:
                    actual = self._text(extra.get("open_library_work_id"))
                if (
                    key == "goodreads"
                    and not actual
                    and row.get("source") == "goodreads"
                ):
                    actual = self._text(row.get("source_id"))
            else:
                actual = self._text(row.get(key))
            if wanted and actual:
                normalized_wanted = (
                    self._canonical_isbn(wanted)
                    if key == "isbn"
                    else re.sub(r"[\s-]+", "", wanted).upper()
                )
                normalized_actual = (
                    self._canonical_isbn(actual)
                    if key == "isbn"
                    else re.sub(r"[\s-]+", "", actual).upper()
                )
                if (
                    normalized_wanted
                    and normalized_actual
                    and normalized_wanted != normalized_actual
                ):
                    return False
                if normalized_wanted and normalized_wanted == normalized_actual:
                    matched_identifier = True
        return matched_identifier or bool(
            title and authors and self._work_description_matches(target, row)
        )

    def _expand_identifiers(
        self,
        log,
        abort,
        provider_ids,
        rows,
        query,
        title,
        authors,
        identifiers,
        timeout,
        goodreads_editions=None,
    ):
        """Follow initial edition and catalog IDs once through compatible providers."""
        support = {
            "asin": {"asintojson", "isbnlab", "audible", "audnexus"},
            "isbn": {
                "isbnlab",
                "goodreads",
                "google_books",
                "hardcover",
                "open_library",
                "internet_archive",
                "new_york_times_books",
            },
            "lccn": {"loc_json_yaml", "open_library"},
            "oclc": {"open_library"},
            "goodreads": {"goodreads"},
            "openlibrary": {"open_library"},
        }
        aliases = {
            "asin": ("asin",),
            "isbn": ("isbn", "isbn13", "isbn10"),
            "lccn": ("lccn",),
            "oclc": ("oclc",),
            "goodreads": ("goodreads",),
            "openlibrary": ("openlibrary",),
        }
        patterns = {
            "asin": r"[A-Z0-9]{10}",
            "isbn": r"(?:\d{13}|\d{9}[\dX])",
            "lccn": r"[A-Z0-9]{4,20}",
            "oclc": r"\d{1,20}",
            "goodreads": r"\d{1,16}",
            "openlibrary": r"OL\d+[MW]",
        }

        def normalize(value):
            return re.sub(r"[\s-]+", "", self._text(value) or "").upper()

        def value_for(row, kind):
            if kind in {"isbn", "asin"}:
                return normalize(row.get(kind))
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            if kind == "goodreads":
                value = extra.get("goodreads_id")
            elif kind == "openlibrary":
                value = extra.get("open_library_edition_id") or extra.get(
                    "open_library_work_id"
                )
            elif kind == "oclc":
                value = extra.get("oclc") or next(
                    iter(self._list(extra.get("oclc_numbers"))), None
                )
            else:
                value = next(iter(self._list(extra.get("lccn"))), None)
            if kind == "goodreads" and not value and row.get("source") == "goodreads":
                value = row.get("source_id")
            return normalize(value)

        initial = {
            kind: normalize(self._identifier(identifiers, *keys))
            for kind, keys in aliases.items()
        }
        seeds = []
        # Goodreads IDs and LCCNs are not sent on the initial generic search.
        for kind in ("goodreads", "lccn", "oclc", "openlibrary"):
            value = initial[kind]
            if value and re.fullmatch(patterns[kind], value):
                seeds.append((kind, value, None))
        for row in rows:
            strict_match = self._matches_requested(row, title, authors, identifiers)
            matches_request = strict_match
            if not strict_match and authors:
                # A catalog may omit the parenthetical series from its title.
                # Use its ISBN to discover corroboration, not to accept its identity.
                suffix = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", self._text(title) or "")
                matches_request = bool(
                    suffix
                    and self._list(row.get("authors"))
                    and self._work_description_matches(
                        {"title": suffix[1], "authors": authors}, row
                    )
                    and self._matches_requested(row, suffix[1], authors, identifiers)
                )
            if not matches_request:
                continue
            for kind in ("isbn", "asin", "goodreads", "lccn", "oclc", "openlibrary"):
                if kind != "isbn" and not strict_match:
                    continue
                value = value_for(row, kind)
                if not value or not re.fullmatch(patterns[kind], value):
                    continue
                if kind == "isbn" and not self._canonical_isbn(value):
                    _log(
                        log,
                        "warning",
                        "Metadata Engine: ignoring invalid ISBN from provider=%s",
                        row.get("source"),
                    )
                    continue
                equivalent = (
                    self._canonical_isbn(value) == self._canonical_isbn(initial[kind])
                    if kind == "isbn" and initial[kind]
                    else value == initial[kind]
                )
                if kind in {"isbn", "asin"} and equivalent:
                    continue
                if not any(item[0] == kind and item[1] == value for item in seeds):
                    seeds.append((kind, value, row))

        # Freeze initial seeds. Results from these lookups never generate more
        # lookups; four IDs and each compatible provider/ID pair are the limit.
        if len(seeds) > 4:
            _log(
                log,
                "warning",
                "Metadata Engine: identifier follow-up limited to 4 of %d discovered IDs",
                len(seeds),
            )
        visited = set()
        for kind, value, anchor in seeds[:4]:
            for provider in provider_ids:
                if abort.is_set():
                    return rows
                if provider not in support[kind]:
                    continue
                if anchor and provider == anchor.get("source"):
                    description = anchor.get("description")
                    has_description = self._usable_description(description)
                    has_tags = self._to_metadata(anchor, 0).tags
                    if kind not in {"goodreads", "openlibrary"}:
                        continue
                    if has_description and has_tags:
                        continue
                pair = (provider, kind, value)
                if pair in visited:
                    continue
                if len(visited) >= 12:
                    _log(
                        log,
                        "warning",
                        "Metadata Engine: identifier follow-up stopped at 12 requests",
                    )
                    return rows
                visited.add(pair)
                try:
                    if kind in {"isbn", "asin"}:
                        found = self._search(
                            provider, query or value, authors, {kind: value}, timeout
                        )
                    else:
                        lookup = "goodreads:" + value if kind == "goodreads" else value
                        if provider == "open_library" and kind in {"lccn", "oclc"}:
                            lookup = kind.upper() + ":" + value
                        found = []
                        anchor_extra = anchor.get("extra") if anchor else None
                        if (
                            kind == "goodreads"
                            and isinstance(anchor_extra, dict)
                            and anchor_extra.get("goodreads_url")
                        ):
                            asin = self._cover_asin(identifiers)
                            if re.fullmatch(r"B[A-Z0-9]{9}", asin):
                                try:
                                    edition = self._goodreads_kindle_edition(
                                        value, asin, min(timeout, 8)
                                    )
                                    if goodreads_editions is not None:
                                        goodreads_editions[value] = edition
                                    if (
                                        edition
                                        and edition["description"]
                                        and edition["genres"]
                                        and self._matches_requested(
                                            edition, title, authors, identifiers
                                        )
                                        and self._compatible_identifiers(
                                            anchor, edition
                                        )
                                        and self._identity_consistent(anchor, edition)
                                    ):
                                        found.append(edition)
                                except (OSError, RuntimeError, ValueError) as exc:
                                    _log(
                                        log,
                                        "warning",
                                        "Metadata Engine Goodreads edition lookup failed: %s",
                                        exc,
                                    )
                        if not found:
                            found = self._search(provider, lookup, None, {}, timeout)
                    accepted = []
                    for candidate in found:
                        candidate_values = {value_for(candidate, kind)}
                        if kind == "isbn":
                            candidate_values = {
                                self._canonical_isbn(item) for item in candidate_values
                            }
                            compare_value = self._canonical_isbn(value)
                        else:
                            compare_value = value
                        if kind == "openlibrary":
                            candidate_extra = (
                                candidate.get("extra")
                                if isinstance(candidate.get("extra"), dict)
                                else {}
                            )
                            candidate_values.add(
                                normalize(candidate_extra.get("open_library_work_id"))
                            )
                        if compare_value not in candidate_values:
                            continue
                        if not self._matches_requested(
                            candidate, title, authors, identifiers
                        ):
                            continue
                        if anchor and (
                            not self._compatible_identifiers(anchor, candidate)
                            or not self._identity_consistent(anchor, candidate)
                        ):
                            _log(
                                log,
                                "warning",
                                "Metadata Engine: rejected conflicting identity evidence provider=%s kind=%s",
                                provider,
                                kind,
                            )
                            continue
                        accepted.append(candidate)
                    rows.extend(accepted)
                    _log(
                        log,
                        "info",
                        "Metadata Engine: identifier follow-up provider=%s kind=%s accepted=%d",
                        provider,
                        kind,
                        len(accepted),
                    )
                except Exception as exc:
                    _log(
                        log,
                        "error",
                        "Metadata Engine identifier follow-up provider %s kind %s failed: %s",
                        provider,
                        kind,
                        exc,
                    )
        return rows

    def _recover_goodreads_subjects(
        self, log, row, asin, abort, timeout, goodreads_editions, *, discover=False
    ):
        """Recover subjects from the verified Kindle edition or its work shelves."""
        extra = row.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        goodreads_id = self._text(extra.get("goodreads_id"))
        if not goodreads_id and row.get("source") == "goodreads":
            goodreads_id = self._text(row.get("source_id"))
        deadline = time.monotonic() + min(timeout, 8)
        try:
            edition = row
            if not re.fullmatch(r"[0-9]{1,16}", goodreads_id or ""):
                if not discover:
                    return
                editions = self._goodreads_work_fallback(
                    self._search_title(row.get("title"), row.get("subtitle")),
                    row.get("authors"),
                    abort,
                    timeout,
                    {"asin": asin},
                )
                edition = next(iter(editions), None)
            elif not extra.get("goodreads_work_id") or self._cover_asin(row) != asin:
                if goodreads_id in goodreads_editions:
                    edition = goodreads_editions[goodreads_id]
                else:
                    edition = self._goodreads_kindle_edition(
                        goodreads_id, asin, min(timeout, 4)
                    )
            if (
                not edition
                or not self._work_description_matches(row, edition)
                or not self._compatible_identifiers(row, edition)
            ):
                return
            genres = self._list(edition.get("genres"))
            work_id = self._text((edition.get("extra") or {}).get("goodreads_work_id"))
            remaining = deadline - time.monotonic()
            if (
                not genres
                and re.fullmatch(r"[0-9]{1,16}", work_id or "")
                and remaining > 0
                and not abort.is_set()
            ):
                genres = self._goodreads_shelf_genres(work_id, min(remaining, 4))
            if genres:
                row["genres"] = list(
                    dict.fromkeys(self._list(row.get("genres")) + genres)
                )
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                log, "info", "Metadata Engine Goodreads subject lookup failed: %s", exc
            )

    def _goodreads_shelf_genres(self, work_id, timeout):
        """Accept established genres from shelf rows and ignore personal labels."""
        names = {
            "biography": "Biography",
            "cozy-mystery": "Cozy Mystery",
            "crime": "Crime",
            "dystopia": "Dystopia",
            "fantasy": "Fantasy",
            "historical-fiction": "Historical Fiction",
            "historical-romance": "Historical Romance",
            "horror": "Horror",
            "memoir": "Memoir",
            "mystery": "Mystery",
            "paranormal": "Paranormal",
            "paranormal-romance": "Paranormal Romance",
            "romance": "Romance",
            "science-fiction": "Science Fiction",
            "space-opera": "Space Opera",
            "superheroes": "Superheroes",
            "superheros": "Superheroes",
            "thriller": "Thriller",
            "urban-fantasy": "Urban Fantasy",
            "western": "Western",
        }
        html = self._http_bytes(
            f"https://www.goodreads.com/work/shelves/{work_id}", timeout
        ).decode("utf-8", "replace")
        shelves = re.findall(
            r"""<div\b[^>]*\bclass=["']shelfStat["'][^>]*>\s*<div\b[^>]*>\s*
                <a\b[^>]*\bhref=["']/genres/([a-z0-9-]+)["']""",
            html,
            re.VERBOSE | re.IGNORECASE,
        )
        return list(dict.fromkeys(names[key] for key in shelves if key in names))

    def _goodreads_kindle_edition(self, goodreads_id, asin, timeout):
        """Read the Goodreads edition page only when its book ID and ASIN agree."""
        row = self._goodreads_book(goodreads_id, timeout)
        return row if row and row.get("asin") == asin else None

    def _recover_catalog_series(
        self, log, rows, title, authors, identifiers, abort, timeout
    ):
        """Split catalog titles only when another source confirms the series and volume."""
        numbers = {
            word: index
            for index, word in enumerate(
                (
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                    "eight",
                    "nine",
                    "ten",
                ),
                1,
            )
        }
        candidates = []
        for row in rows:
            display = self._text(row.get("title")) or ""
            subtitle = self._text(row.get("subtitle"))
            if subtitle and subtitle.casefold() not in display.casefold():
                display += f": {subtitle}"
            match = re.fullmatch(
                r"(.+?)\s*:\s*([^:]+?)\s*:\s*(?:book\s+)?(\d+(?:\.\d+)?|"
                + "|".join(numbers)
                + r")",
                display,
                re.IGNORECASE,
            )
            if not match:
                continue
            base, name, number = match.groups()
            work = dict(row, title=base, subtitle=None)
            if self._matches_requested(work, title, authors, identifiers):
                position = numbers.get(number.casefold(), number)
                candidates.append(
                    (row, work, self._series_name_key(name.casefold()), float(position))
                )
        if not candidates or abort.is_set():
            return
        try:
            donors = self._goodreads_work_fallback(
                candidates[0][1]["title"], authors, abort, timeout
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _log(
                log,
                "warning",
                "Metadata Engine Goodreads series lookup failed: %s",
                exc,
            )
            return
        for donor in donors:
            name, position = self._series_identity(donor)
            series = self._series_name_key(name), position
            work_fields = {
                key: donor[key] for key in ("title", "authors", "genres", "series")
            }
            for row, work, expected_name, expected_position in candidates:
                if series == (
                    expected_name,
                    expected_position,
                ) and self._merge_metadata(work, work_fields):
                    if self._series_identity(work)[1] is None:
                        work["series"] = donor["series"]
                    row.update(work)
                    _log(
                        log,
                        "info",
                        "Metadata Engine corroborated catalog series using Goodreads: %s",
                        work["title"],
                    )

    def _goodreads_work_fallback(
        self, title, authors, abort, timeout, identifiers=None
    ):
        """Recover work metadata or a verified Kindle edition from Goodreads."""
        authors = self._list(authors)
        if not title or not authors or abort.is_set():
            return []

        def title_key(value):
            normalized, _, _ = self._work_title(value)
            return " ".join(re.sub(r"[:,;]", " ", normalized).split())

        query = re.sub(r"\s+\((?:18|19|20)\d{2}\)$", "", self._search_title(title))
        query = re.sub(r"\s*;\s*", ": ", query)
        if not query:
            return []
        started = time.monotonic()
        deadline = started + min(timeout, 8)
        asin = self._cover_asin(identifiers or {})
        saved_id = self._identifier(
            identifiers or {}, "goodreads", "metadata_engine_goodreads"
        )
        alias = self._verified_kindle_title_alias(title, authors, asin)
        if alias:
            original, catalog, position = alias
            deadline = started + min(timeout, 12)
            ids = (
                [saved_id]
                if re.fullmatch(r"[0-9]{1,16}", saved_id or "")
                else (self._goodreads_suggestions(asin, min(timeout, 5)))
            )
            for book_id in list(ids)[:3]:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or abort.is_set():
                    break
                row = self._goodreads_book(book_id, remaining)
                if (
                    row
                    and self._work_title(row["title"])[0]
                    in {catalog, original.casefold()}
                    and self._series_identity(row)
                    == ("kira & nathaniel's duet", position)
                ):
                    target = {
                        "title": original,
                        "authors": authors,
                        "series": row["series"],
                    }
                    verified = self._verify_goodreads_edition(
                        dict(row, title=original), target, identifiers
                    )
                    if verified:
                        return [dict(verified, verified_original_title=True)]
            return []
        if re.fullmatch(r"[0-9]{1,16}", saved_id or "") and re.fullmatch(
            r"B[A-Z0-9]{9}", asin
        ):
            return self._goodreads_saved_kindle_fallback(
                saved_id, title, authors, identifiers, abort, deadline
            )
        base, separator, _ = query.partition(": ")
        decorated_kindle = bool(separator and re.fullmatch(r"B[A-Z0-9]{9}", asin))
        sorted_kindle = bool(
            re.fullmatch(r"B[A-Z0-9]{9}", asin)
            and self._restore_title_article(title) != title
        )
        if sorted_kindle:
            deadline = started + min(timeout, 20)
            ids = self._goodreads_suggestions(asin, min(timeout, 10))
            remaining = deadline - time.monotonic()
            if not ids and remaining > 0 and not abort.is_set():
                ids = self._goodreads_suggestions(
                    f"{query} {authors[0]}", min(remaining, 4)
                )
        else:
            ids = self._goodreads_suggestions(f"{query} {authors[0]}", min(timeout, 4))
        remaining = deadline - time.monotonic()
        if (
            not ids
            and decorated_kindle
            and not sorted_kindle
            and remaining > 0
            and not abort.is_set()
        ):
            # Marketing subtitles may be absent from the catalog's search index.
            deadline = started + min(timeout, 12)
            ids = self._goodreads_suggestions(asin, min(remaining, 4))
        series, position = self._series_identity({"title": title})
        for goodreads_id in list(ids)[:3]:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or abort.is_set():
                break
            row = self._goodreads_book(
                goodreads_id, min(remaining, 12 if sorted_kindle else 4)
            )
            if not row:
                continue
            catalog_title = title_key(row["title"])
            if (
                catalog_title != title_key(query)
                and not (
                    decorated_kindle
                    and row.get("asin") == asin
                    and catalog_title == title_key(base)
                )
                and not (
                    sorted_kindle
                    and self._work_titles_compatible(
                        {"title": query, "asin": asin}, row
                    )
                )
            ):
                continue
            if (
                not series
                and decorated_kindle
                and row.get("asin") == asin
                and row.get("series")
                and catalog_title == title_key(base)
            ):
                # The verified edition can carry a separate collection label in
                # its display title. Prefer its structured series and position.
                row = dict(row, title=self._search_title(row["title"]))
            target = {"title": row["title"], "authors": authors}
            if series:
                target["series"] = [{"name": series, "position": position}]
            if identifiers:
                verified = self._verify_goodreads_edition(row, target, identifiers)
                if verified:
                    return [verified]
                continue
            if self._identity_consistent(target, row):
                # A title-only lookup establishes the work but cannot verify an
                # edition's identifiers, cover, or publication details.
                return [
                    {
                        key: row[key]
                        for key in (
                            "title",
                            "authors",
                            "description",
                            "genres",
                            "series",
                            "source",
                            "source_id",
                            "extra",
                        )
                    }
                ]
        return []

    @classmethod
    def _verified_kindle_title_alias(cls, title, authors, asin):
        """Recognize documented title changes for the original Kindle editions."""
        # NetGalley 308725 and 459589 document the original title and reissue.
        # StoryGraph series 2457600 corroborates both original Kindle ASINs.
        # Goodreads edition 197349413 documents the second title change.
        aliases = {
            "B0BZT83XHG": ("Plug and Claim", "collared by the vampire prince", 1),
            "B0CG4KBN38": ("Plug and Tame", "groomed by the vampire prince", 2),
        }
        alias = aliases.get(asin)
        if not alias or [cls._author_words(name) for name in authors] != [
            ("darcy", "fayton")
        ]:
            return None
        original, _, position = alias
        name, series, number = cls._work_title(title)
        if name.partition(": ")[0] != original.casefold():
            return None
        if series and (
            series not in {"plug and claim duology", "kira & nathaniel's duet"}
            or number != position
        ):
            return None
        return alias

    def _goodreads_saved_kindle_fallback(
        self, book_id, title, authors, identifiers, abort, deadline
    ):
        """Use a saved record directly and corroborate a missing work separately."""
        remaining = deadline - time.monotonic()
        if remaining <= 0 or abort.is_set():
            return []
        target = {"title": title, "authors": authors}
        try:
            row = self._goodreads_book(book_id, min(remaining, 4))
        except (OSError, RuntimeError, ValueError):
            # A blocked or unreachable Goodreads page cannot verify the edition.
            # Fall back to independent work evidence without replacing its IDs.
            row = None
        if row:
            if not self._work_description_matches(target, row):
                # A publisher subtitle may be missing from the saved edition's
                # catalog title. It is safe only with the exact saved ASIN.
                base, separator, _ = title.partition(": ")
                if not (
                    separator
                    and row.get("asin") == self._cover_asin(identifiers)
                    and self._work_description_matches(dict(target, title=base), row)
                    and self._work_title(row["title"])[0] == self._work_title(base)[0]
                ):
                    return []
                name, position = self._series_identity(target)
                if name:
                    target["series"] = [{"name": name, "position": position}]
            verified = self._verify_goodreads_edition(
                row, dict(target, title=row["title"]), identifiers
            )
            return [verified] if verified else []
        remaining = deadline - time.monotonic()
        if remaining <= 0 or abort.is_set() or len(authors) != 1:
            return []
        try:
            works = self._book_notification_work(target, min(remaining, 4))
        except (OSError, RuntimeError, ValueError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise RuntimeError(f"Book Notification unavailable: {reason}") from exc
        return [
            self._with_curated_work_fields(work, self._cover_asin(identifiers))
            for work in works
        ]

    @classmethod
    def _with_curated_work_fields(cls, work, asin):
        """Fill work fields for a verified bibliography entry with a retired edition."""
        # Romance.io's book-one entry supplies the plot and subject labels.
        # https://www.romance.io/books/65e7110ceb892d0f32d6993b/henrikks-punishment-darcy-fayton
        if (
            asin != "B0CVZJHQ6W"
            or work.get("source") != "book_notification"
            or work.get("source_id") != "3886230"
            or cls._work_title(work.get("title"))[0] != "henrikk's punishment"
            or [cls._author_words(author) for author in cls._list(work.get("authors"))]
            != [("darcy", "fayton")]
            or cls._series_identity(work) != ("henrikk's short stories", 1.0)
        ):
            return work
        return dict(
            work,
            description=cls._usable_description(work.get("description"))
            or (
                "General Asher takes the blame for a crime to protect someone else. "
                "When Vampire King Henrikk punishes him before the court, the encounter "
                "forces both men to reckon with feelings they have kept hidden. "
                "Asher must decide whether he can trust the king as betrayal threatens "
                "their bond."
            ),
            genres=list(
                dict.fromkeys(
                    [
                        *cls._list(work.get("genres")),
                        "Vampires",
                        "Dark Romance",
                        "Gay Romance",
                        "Fantasy",
                    ]
                )
            ),
        )

    def _book_notification_work(self, target, timeout):
        """Verify an unavailable catalog work against a single-author bibliography."""
        from lxml.html import fromstring

        author = target["authors"][0]
        words = self._author_words(author)
        if len(words) < 2 or not all(re.fullmatch(r"[a-z]+", word) for word in words):
            return []
        slug = "-".join(words)
        payload = self._http_bytes(
            f"https://www.booknotification.com/authors/{slug}/", timeout
        )
        if not payload.strip():
            return []
        document = fromstring(payload)
        headings = document.xpath("//h1[@id='bnauthname']")
        if (
            len(headings) != 1
            or self._author_words(headings[0].text_content()) != words
        ):
            return []
        matches = []
        for table in document.xpath("//table[starts-with(@id, 'books')]"):
            headings = table.xpath("preceding::h3[@class='series_title'][1]")
            if not headings:
                continue
            series = re.fullmatch(
                r"List of (.+) Books in Publication Order",
                headings[0].text_content().strip(),
            )
            if not series:
                continue
            for entry in table.xpath(".//tr[td//div[@class='titles']/a]"):
                links = entry.xpath("./td//div[@class='titles']/a")
                if len(links) != 1:
                    continue
                position = entry.xpath("string(./td[1])").strip()
                source_id = re.fullmatch(r"hide([0-9]+)", entry.get("id") or "")
                if not source_id or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", position):
                    continue
                row = {
                    "source": "book_notification",
                    "source_id": source_id[1],
                    "title": links[0].text_content().strip(),
                    "authors": [author],
                    "series": [{"name": series[1], "position": float(position)}],
                }
                if self._work_description_matches(target, row):
                    matches.append(row)
        return matches if len(matches) == 1 else []

    def _goodreads_suggestions(self, query, timeout):
        """Read unique Goodreads book IDs from autocomplete in catalog order."""
        params = urlencode({"q": query, "format": "json"})
        suggestions = self._http_bytes(
            f"https://www.goodreads.com/book/auto_complete?{params}", timeout
        ).decode("utf-8", "replace")
        if suggestions.lstrip().startswith("["):
            return dict.fromkeys(
                value
                for item in json.loads(suggestions)
                if isinstance(item, dict)
                and re.fullmatch(r"\d{1,16}", value := str(item.get("bookId", "")))
            )
        return dict.fromkeys(re.findall(r'\bbook_id=["\'](\d{1,16})["\']', suggestions))

    def _verify_goodreads_edition(self, row, target, identifiers):
        """Require the Kindle ASIN and corroborate alternate anthology credits."""
        if row.get("asin") != self._cover_asin(identifiers):
            return None
        if not self._identity_consistent(target, row) and not (
            self._anthology_contributors_match(target["authors"], row["authors"])
            and self._identity_consistent(dict(target, authors=[]), row)
        ):
            return None
        credits = {}
        for name in [*target["authors"], *row["authors"]]:
            # Avoid duplicate credits when a catalog adds accents to a name.
            folded = unicodedata.normalize("NFKD", name)
            key = self._author_words(
                "".join(c for c in folded if not unicodedata.combining(c))
            )
            credits.setdefault(key, name)
        verified = dict(row, authors=list(credits.values()))
        if self._matches_requested(
            verified, target["title"], target["authors"], identifiers
        ):
            return verified
        return None

    def _goodreads_book(self, goodreads_id, timeout):
        """Read work and edition fields from the requested Goodreads book page."""
        url = f"https://www.goodreads.com/en/book/show/{goodreads_id}"
        html = self._http_bytes(url, timeout).decode("utf-8", "replace")
        script = re.search(
            r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not script:
            return None
        data = json.loads(script[1])
        if not isinstance(data, dict):
            return None
        props = data.get("props")
        page = props.get("pageProps") if isinstance(props, dict) else None
        state = page.get("apolloState") if isinstance(page, dict) else None
        if not isinstance(state, dict):
            return None

        def linked(edge):
            key = edge.get("__ref") if isinstance(edge, dict) else None
            record = state.get(key) if isinstance(key, str) else None
            return record if isinstance(record, dict) else {}

        for book in state.values():
            if (
                not isinstance(book, dict)
                or book.get("__typename") != "Book"
                or str(book.get("legacyId")) != goodreads_id
            ):
                continue
            details = book.get("details")
            if not isinstance(details, dict):
                details = {}
            contributors = [book.get("primaryContributorEdge")]
            contributors.extend(book.get("secondaryContributorEdges") or [])
            title = self._text(book.get("titleComplete") or book.get("title"))
            authors = list(
                dict.fromkeys(
                    name
                    for edge in contributors
                    if isinstance(edge, dict)
                    and edge.get("role") in (None, "Author", "Co-Author")
                    and (name := self._text(linked(edge.get("node")).get("name")))
                )
            )
            if not title or not authors:
                return None
            genres = [
                genre["name"]
                for entry in book.get("bookGenres") or []
                if isinstance(entry, dict)
                and isinstance(genre := entry.get("genre"), dict)
                and self._text(genre.get("name"))
            ]
            series = []
            for entry in book.get("bookSeries") or []:
                if isinstance(entry, dict):
                    position = entry.get("userPosition")
                    # An included story's series does not belong to the box set.
                    if re.search(r"\bincluded\b", str(position), re.IGNORECASE):
                        continue
                    name = self._text(linked(entry.get("series")).get("title"))
                    if name:
                        series.append({"name": name, "position": position})
            # Goodreads sometimes prepends a catalog status to the old
            # edition's title. Keep the actual title and series for matching.
            title = re.sub(
                r"^(?:old\s+)?retired\s+edition\s*[-–—:]\s*"
                r"(?:not\s+the\s+\d{4}\s+rewrite\s*[-–—:]\s*)?",
                "",
                unescape(title),
                flags=re.IGNORECASE,
            )
            if not title:
                return None
            work = linked(book.get("work"))
            return {
                "source": "goodreads",
                "source_id": goodreads_id,
                "title": title,
                "authors": authors,
                "asin": self._text(details.get("asin")),
                "isbn": self._canonical_isbn(
                    details.get("isbn13") or details.get("isbn")
                ),
                "description": self._usable_description(book.get("description")),
                "genres": genres,
                "series": series,
                "publisher": self._text(details.get("publisher")),
                "cover_url": self._text(book.get("imageUrl")),
                "extra": {
                    "goodreads_id": goodreads_id,
                    "goodreads_url": url,
                    "goodreads_work_id": self._text(work.get("legacyId")),
                },
            }
        return None

    def _recover_descriptions(
        self,
        log,
        abort,
        provider_ids,
        rows,
        query,
        title,
        authors,
        identifiers,
        timeout,
        retried_providers=None,
    ):
        """Retry configured title-search providers for an incomplete matching work."""

        def requested_match(row):
            return self._matches_requested(row, title, authors, identifiers)

        def has_description(row):
            return bool(self._usable_description(row.get("description")))

        matches = [row for row in self._dedupe(rows) if requested_match(row)]
        missing = [row for row in matches if not has_description(row)]
        if not missing:
            return rows
        candidate = missing[0]
        short_title = self._search_title(candidate.get("title"))
        if not short_title or not self._list(candidate.get("authors")):
            _log(
                log,
                "warning",
                "Metadata Engine: matching result has no description; insufficient title or author data for recovery",
            )
            return rows

        _log(
            log,
            "info",
            "Metadata Engine: matching result has no description; trying configured sources",
        )
        retry_ids = [
            provider
            for provider in provider_ids
            if provider not in {"asintojson", "isbnlab"}
            and provider not in (retried_providers or set())
        ]
        if not authors and not identifiers and query.strip() == short_title:
            return rows
        # One bounded refinement batch. Independent sources start together;
        # merging stays serial and retains the normal identity checks.
        retry_batch = self._initial_provider_batch(
            retry_ids, short_title, None, {}, min(timeout, 5), abort
        )
        for provider_id in retry_ids:
            if abort.is_set():
                return rows
            try:
                mode = "books" if provider_id == "itunes" else None
                found, error = retry_batch[(provider_id, mode)]
                if error is not None:
                    raise error
                _log(
                    log,
                    "info",
                    "Metadata Engine: description retry provider=%r candidates=%d",
                    provider_id,
                    len(found),
                )
                for winner in rows:
                    if not requested_match(winner):
                        continue
                    for donor in found:
                        before = has_description(winner)
                        if self._merge_metadata(winner, donor):
                            if not before and has_description(winner):
                                _log(
                                    log,
                                    "info",
                                    "Metadata Engine: description supplied by provider=%r",
                                    provider_id,
                                )
                        elif has_description(donor):
                            reason = (
                                "conflicting identifiers"
                                if not self._compatible_identifiers(winner, donor)
                                else "title or explicit edition conflict"
                                if not self._work_titles_compatible(winner, donor)
                                else "contributor, series or edition evidence conflict"
                            )
                            _log(
                                log,
                                "info",
                                "Metadata Engine: rejected description provider=%s reason=%s isbn=%s",
                                provider_id,
                                reason,
                                donor.get("isbn"),
                            )
                if not any(
                    requested_match(row) and not has_description(row)
                    for row in self._dedupe(rows)
                ):
                    _log(log, "info", "Metadata Engine: matching description recovered")
                    return rows
            except Exception as exc:
                _log(
                    log,
                    "error",
                    "Metadata Engine description retry provider %r failed: %s",
                    provider_id,
                    exc,
                )
        _log(
            log,
            "warning",
            "Metadata Engine: matching result remains without a description after configured sources",
        )
        return rows

    def download_cover(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers=None,
        timeout=30,
        get_best_cover=False,
    ):
        """Prefer the requested format and cover shape before image size."""
        identifiers = identifiers or {}
        audiobook = self._audiobook_cover_lookup(identifiers)
        candidates = self._kindle_cover_candidates(identifiers)
        google_id = self._identifier(
            identifiers, "metadata_engine_google_books", "google"
        )
        if google_id and re.fullmatch(r"[A-Za-z0-9_-]{6,32}", google_id):
            candidates.append(
                {
                    "source": "google_books",
                    "source_id": google_id,
                    "metadata_type": "books",
                    **self._native_google_cover(google_id),
                }
            )

        cached = self._cached_cover(identifiers)
        if cached:
            candidates.append(
                {
                    "cover_url": cached,
                    "confidence": 1.0,
                    "source": "cache",
                }
            )

        query = self._make_query(title, authors, identifiers)
        if query:
            target = {
                "title": title,
                "authors": authors,
                "isbn": self._identifier(identifiers, "isbn", "isbn13", "isbn10"),
                "asin": self._cover_asin(identifiers),
            }
            provider_ids = self._book_provider_ids(log, min(timeout, 5), abort)

            initial = self._initial_provider_batch(
                provider_ids, query, authors, identifiers, min(timeout, 8), abort
            )
            confirmed = any(
                row.get("cover_url")
                and self._cover_identifier_score(row, identifiers) > 1
                and self._identity_consistent(target, row)
                for provider_rows, error in initial.values()
                if error is None
                for row in provider_rows
            )
            retry_deadline = time.monotonic() + min(timeout, 5)
            rows = []
            for provider_id in provider_ids:
                if abort.is_set():
                    return None
                try:
                    provider_rows, error = initial[(provider_id, None)]
                    if error is not None:
                        raise error
                    rows.extend(provider_rows)
                    if provider_id == "itunes":
                        books, error = initial[(provider_id, "books")]
                        if error is not None:
                            raise error
                        rows.extend(books)
                        provider_rows.extend(books)
                    if (
                        title
                        and not confirmed
                        and len(self._list(authors)) > 1
                        and provider_id
                        in {
                            "goodreads",
                            "google_books",
                            "hardcover",
                            "open_library",
                            "itunes",
                        }
                        and not any(
                            self._work_description_matches(target, item)
                            for item in provider_rows
                        )
                    ):
                        for person in [None, *self._list(authors)[:3]]:
                            if abort.is_set():
                                return None
                            remaining = retry_deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            retry = self._search(
                                provider_id,
                                self._search_title(title),
                                [person] if person else None,
                                {},
                                remaining,
                                **(
                                    {"metadata_type": "books"}
                                    if provider_id == "itunes"
                                    else {}
                                ),
                            )
                            matching = [
                                item
                                for item in retry
                                if self._work_description_matches(target, item)
                            ]
                            _log(
                                log,
                                "info",
                                "Metadata Engine cover retry provider=%s candidates=%d matches=%d",
                                provider_id,
                                len(retry),
                                len(matching),
                            )
                            if matching:
                                rows.extend(matching)
                                provider_rows.extend(matching)
                                break
                    cover_count = sum(
                        1 for row in provider_rows if row.get("cover_url")
                    )
                    if cover_count:
                        _log(
                            log,
                            "info",
                            "Metadata Engine provider %r returned %d cover candidate(s)",
                            provider_id,
                            cover_count,
                        )
                except Exception as exc:
                    # A cover lookup is an aggregate provider operation. One provider
                    # failing (for example NYT returning 502) must not suppress covers
                    # returned by Open Library, Google Books, Hardcover, etc.
                    _log(
                        log,
                        "error",
                        "Metadata Engine cover provider %r failed: %s",
                        provider_id,
                        exc,
                    )

            rows.extend(
                self._recover_cover_records(
                    log,
                    abort,
                    provider_ids,
                    rows,
                    title,
                    authors,
                    identifiers,
                    min(timeout, 5),
                )
            )

            audiobook = self._audiobook_cover_lookup(
                identifiers,
                (
                    row
                    for row in rows
                    if self._cover_identifier_score(row, identifiers) > 1
                    and self._identity_consistent(target, row)
                ),
            )
            wanted_media = "audiobook" if audiobook else "book"
            eligible = []
            missing_cover = rejected_identity = 0
            for row in rows:
                if not row.get("cover_url"):
                    missing_cover += 1
                    continue
                score = self._cover_identifier_score(row, identifiers)
                if (
                    not score
                    or self._cover_media(row) not in ("", wanted_media)
                    or not self._identity_consistent(target, row)
                    or not (
                        score > 1
                        or (
                            title
                            and authors
                            and self._work_description_matches(target, row)
                        )
                    )
                ):
                    rejected_identity += 1
                    continue
                eligible.append(row)
            _log(
                log,
                "info",
                "Metadata Engine cover: records=%d missing_cover=%d rejected_identity=%d eligible=%d",
                len(rows),
                missing_cover,
                rejected_identity,
                len(eligible),
            )
            rows = eligible
            rows.sort(
                key=lambda x: (
                    self._cover_identifier_score(x, identifiers),
                    float(x.get("confidence") or 0),
                ),
                reverse=True,
            )
            if not any(row.get("source") == "kindle" for row in candidates):
                for row in rows:
                    if self._cover_identifier_score(row, identifiers) == 3:
                        candidates.extend(
                            dict(item, isbn=row["isbn"])
                            for item in self._kindle_cover_candidates(row)
                        )
            if not audiobook and "itunes" in provider_ids and not abort.is_set():
                try:
                    candidates.extend(
                        self._apple_cover_candidates(target, min(timeout, 4))
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    _log(
                        log,
                        "warning",
                        "Metadata Engine Apple ISBN lookup failed: %s",
                        exc,
                    )
            if not google_id and not audiobook:
                for row in rows:
                    volume = self._text(row.get("source_id")) or ""
                    if (
                        row.get("source") == "google_books"
                        and re.fullmatch(r"[A-Za-z0-9_-]{6,32}", volume)
                        and self._work_description_matches(target, row)
                    ):
                        candidates.append(
                            dict(
                                row,
                                **self._native_google_cover(volume, "/books/content"),
                            )
                        )
                        break
            candidates.extend(rows)

        from qt.core import QImage

        seen_urls = set()
        requests = []
        candidates.sort(
            key=lambda row: (
                row.get("source") == "kindle",
                self._cover_identifier_score(row, identifiers),
            ),
            reverse=True,
        )
        # Bound network work while still allowing several sources and sizes.
        for row in candidates:
            if abort.is_set():
                return None
            if len(requests) >= 12:
                break
            if self._cover_media(row) not in ("", "audiobook" if audiobook else "book"):
                continue
            url = self._text(row.get("cover_url"))
            if not url:
                continue
            urls = [url]
            host = (urlsplit(url).hostname or "").casefold()
            if row.get("native_google_cover"):
                urls.append(url.removesuffix("0") + "1")
            elif host == "mzstatic.com" or host.endswith(".mzstatic.com"):
                size = re.search(r"/(\d+)x(\d+)bb(?=\.)", url)
                if size and max(int(size[1]), int(size[2])) < 10000:
                    urls.insert(
                        0, url[: size.start()] + "/10000x10000bb" + url[size.end() :]
                    )
            urls = [item for item in urls if item not in seen_urls]
            if urls:
                seen_urls.update(urls)
                requests.append((row, urls))

        def download(request):
            row, urls = request
            best = None
            for candidate_url in urls:
                if abort.is_set():
                    return None
                try:
                    data = self._http_bytes(candidate_url, min(timeout, 10))
                    if not self._looks_like_image(data):
                        raise ValueError("downloaded content is not a recognized image")
                    image = QImage.fromData(data)
                    if image.isNull() or image.width() <= 0 or image.height() <= 0:
                        raise ValueError("downloaded image cannot be decoded")
                    if (
                        row.get("source") == "kindle" or row.get("native_google_cover")
                    ) and min(image.width(), image.height()) < 100:
                        raise ValueError("Image is too small to be a cover")
                    if row.get("native_google_cover") and md5(
                        data, usedforsecurity=False
                    ).hexdigest() in {
                        "0de4383ebad0adad5eeb8975cd796657",
                        "a64fa89d7ebc97075c1d363fc5fea71f",
                    }:
                        raise ValueError("Google Books returned a placeholder image")
                    score = self._cover_identifier_score(row, identifiers)
                    _log(
                        log,
                        "info",
                        "Metadata Engine cover candidate source=%s size=%dx%d identifier_score=%d",
                        self._text(row.get("source")) or "unknown",
                        image.width(),
                        image.height(),
                        score,
                    )
                    shape = self._cover_aspect_rank(
                        image.width(), image.height(), audiobook
                    )
                    signature = self._cover_signature(image)
                    illustrated = not self._mostly_white_cover(signature)
                    rank = (
                        illustrated,
                        shape,
                        score > 1,
                        image.width() * image.height(),
                        score,
                    )
                    if best is None or rank > best[0]:
                        best = (rank, row, candidate_url, data, signature)
                    if row.get("native_google_cover") and shape == 2 and illustrated:
                        break
                except Exception as exc:
                    _log(
                        log,
                        "error",
                        "Metadata Engine cover candidate failed source=%r url=%s error=%s",
                        row.get("source"),
                        candidate_url,
                        exc,
                    )
            return best

        covers = []
        if requests:
            with ThreadPoolExecutor(max_workers=min(len(requests), 12)) as executor:
                covers = [item for item in executor.map(download, requests) if item]

        if covers:
            preferred = max(item[0][:2] for item in covers)
            # Calibre sorts multi-cover results by area. Filter fallback pages
            # and less suitable shapes before the results leave this plugin.
            covers = [item for item in covers if item[0][:2] == preferred]
            covers.sort(key=lambda item: (item[0][3], item[0][4]), reverse=True)
            unique = []
            for cover in covers:
                if not any(self._same_cover_art(cover[4], item[4]) for item in unique):
                    unique.append(cover)
            unique.sort(key=lambda item: item[0], reverse=True)
            winner = unique[0]
            for _, _, _, data, _ in unique:
                if abort.is_set():
                    return None
                result_queue.put((self, data))
                if get_best_cover:
                    break
            self._remember_cover(dict(winner[1], cover_url=winner[2]))
            return None

        if candidates:
            _log(
                log,
                "error",
                "Metadata Engine exhausted %d cover candidate(s)",
                len(candidates),
            )
        else:
            _log(log, "error", "Metadata Engine returned no cover candidates")
        return None

    @staticmethod
    def _native_google_cover(volume, path="/books"):
        """Build a native-size cover request for a verified Google Books volume."""
        return {
            "native_google_cover": True,
            "cover_url": (
                f"https://books.google.com{path}?id={volume}"
                "&printsec=frontcover&img=1&zoom=0"
            ),
        }

    @staticmethod
    def _cover_signature(image):
        """Sample decoded color and aspect independently of encoding and resolution."""
        from qt.core import QImage, Qt

        sample = image.scaled(
            32,
            32,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_RGB888)
        return image.width() / image.height(), sample.constBits().asstring(
            sample.sizeInBytes()
        )

    @staticmethod
    def _mostly_white_cover(signature):
        """Recognize near-white pages with little artwork as fallback images."""
        _, pixels = signature
        white = sum(
            min(pixel) >= 235 and max(pixel) - min(pixel) <= 14
            for pixel in zip(pixels[::3], pixels[1::3], pixels[2::3])
        )
        return white >= 0.95 * (len(pixels) // 3)

    @staticmethod
    def _same_cover_art(first, second):
        """Match copies with minor crop, color, or compression differences."""
        first_aspect, first_pixels = first
        second_aspect, second_pixels = second
        if max(first_aspect, second_aspect) > min(first_aspect, second_aspect) * 1.08:
            return False
        differences = [abs(a - b) for a, b in zip(first_pixels, second_pixels)]
        return sum(differences) <= 11 * len(differences) and sum(
            value > 35 for value in differences
        ) <= 0.06 * len(differences)

    @staticmethod
    def _cover_aspect_rank(width, height, audiobook):
        """Rank decoded shapes for the requested kind of publication."""
        if audiobook:
            return 2 if width == height else int(width > height)
        return 2 if height > width else 0

    @classmethod
    def _cover_media(cls, row):
        """Recognize explicit publication formats without guessing from artwork."""
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        physical = str(
            extra.get("physical_format") or extra.get("format") or ""
        ).casefold()
        if "audio" in physical or row.get("source") in {"audible", "audnexus"}:
            return "audiobook"
        if any(
            word in physical
            for word in (
                "kindle",
                "ebook",
                "e-book",
                "paperback",
                "hardcover",
                "hardback",
            )
        ):
            return "book"
        kind = str(row.get("metadata_type") or "").strip().casefold()
        if kind in {"audiobook", "audiobooks"}:
            return "audiobook"
        return "book" if kind and cls._book_media(row) else ""

    @classmethod
    def _audiobook_cover_lookup(cls, identifiers, verified_rows=()):
        """Use saved media or an unambiguous exact-edition format match."""
        media = cls._identifier(identifiers, "metadata_engine_media")
        if media in {"book", "audiobook"}:
            return media == "audiobook"
        if cls._identifier(
            identifiers,
            "audible",
            "audnexus",
            "metadata_engine_audible",
            "metadata_engine_audnexus",
        ):
            return True
        known = {cls._cover_media(row) for row in verified_rows}
        return "audiobook" in known and "book" not in known

    def _apple_cover_candidates(self, target, timeout):
        """Find Apple Books artwork for the exact ISBN before ranking covers."""
        isbn = self._canonical_isbn(target.get("isbn"))
        if not isbn:
            return []
        params = urlencode(
            {
                "isbn": isbn,
                "entity": "ebook",
                "limit": 10,
                "version": 2,
                "country": "US",
            }
        )
        payload = self._http_json(f"https://itunes.apple.com/lookup?{params}", timeout)
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        covers = []
        for item in results:
            if not isinstance(item, dict) or item.get("kind") != "ebook":
                continue
            url = urlsplit(self._text(item.get("artworkUrl100")))
            if url.scheme != "https" or not (url.hostname or "").endswith(
                ".mzstatic.com"
            ):
                continue
            row = {
                "source": "itunes",
                "title": self._text(item.get("trackName")),
                "authors": self._list(item.get("artistName")),
                "isbn": isbn,
                "cover_url": url.geturl(),
            }
            if not row["title"] or not row["authors"]:
                continue
            if self._identity_consistent(target, row):
                covers.append(row)
                continue
            title, separator, subtitle = row["title"].partition(": ")
            if separator:
                # Apple sometimes puts the franchise after the work title.
                reversed_title = dict(row, title=f"{subtitle}: {title}")
                if self._work_description_matches(
                    target, reversed_title
                ) and self._identity_consistent(target, reversed_title):
                    covers.append(reversed_title)
        return covers

    @classmethod
    def _kindle_cover_candidates(cls, identifiers):
        """Use the native Amazon image for an explicitly identified Kindle edition."""
        asin = cls._cover_asin(identifiers)
        if not re.fullmatch(r"B[A-Z0-9]{9}", asin):
            return []
        row = {
            "asin": asin,
            "source": "kindle",
            "cover_url": (
                f"https://ec2.images-amazon.com/images/P/{asin}.01.MAIN._SCRM_.jpg"
            ),
        }
        return [row]

    @classmethod
    def _cover_asin(cls, identifiers):
        """Read saved Kindle and regional Amazon identifiers without guessing an ASIN."""
        explicit = cls._identifier(identifiers, "asin")
        if explicit:
            return explicit.upper()
        amazon_ids = {
            str(value).strip().upper()
            for key, value in identifiers.items()
            if (
                str(key).lower().startswith("amazon")
                or str(key).lower()
                in {
                    "audible",
                    "audnexus",
                    "metadata_engine_audible",
                    "metadata_engine_audnexus",
                }
            )
            and re.fullmatch(r"B[A-Z0-9]{9}", str(value).strip(), re.IGNORECASE)
        }
        return next(iter(amazon_ids)) if len(amazon_ids) == 1 else ""

    def _recover_cover_records(
        self,
        log,
        abort,
        provider_ids,
        initial_rows,
        title,
        authors,
        identifiers,
        timeout,
    ):
        """Follow bounded provider IDs once, accepting only verified cover records."""
        # Each entry describes an explicitly supported provider's direct lookup.
        # Follow-ups use only the frozen initial input; results are never recursed.
        strategies = (
            (
                "goodreads",
                ("goodreads", "metadata_engine_goodreads"),
                "goodreads_id",
                r"\d{1,16}",
                "goodreads:",
            ),
        )
        recovered = []
        deadline = time.monotonic() + timeout
        target = {
            "title": title,
            "authors": authors,
            "isbn": self._identifier(identifiers, "isbn", "isbn13", "isbn10"),
        }
        visited = set()
        for provider, aliases, extra_key, pattern, prefix in strategies:
            if provider not in provider_ids:
                continue
            requested = self._identifier(identifiers, *aliases)
            ids = [requested] if requested else []
            for row in initial_rows:
                if row.get("source") != provider:
                    continue
                extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
                value = self._text(extra.get(extra_key) or row.get("source_id"))
                if (
                    value
                    and (not requested or value == requested)
                    and self._identity_consistent(target, row)
                ):
                    ids.append(value)
            for value in list(dict.fromkeys(ids))[:4]:
                remaining = deadline - time.monotonic()
                if abort.is_set() or remaining <= 0:
                    return recovered
                pair = (provider, value)
                if pair in visited:
                    continue
                visited.add(pair)
                if not re.fullmatch(pattern, value):
                    _log(
                        log,
                        "warning",
                        "Metadata Engine cover: invalid provider ID provider=%s",
                        provider,
                    )
                    continue
                try:
                    found = self._search(provider, prefix + value, None, {}, remaining)
                    accepted = 0
                    for row in found:
                        extra = (
                            row.get("extra")
                            if isinstance(row.get("extra"), dict)
                            else {}
                        )
                        returned = self._text(
                            extra.get(extra_key) or row.get("source_id")
                        )
                        if (
                            returned != value
                            or not self._identity_consistent(target, row)
                            or not self._cover_identifier_score(row, identifiers)
                        ):
                            _log(
                                log,
                                "warning",
                                "Metadata Engine cover: rejected conflicting provider record provider=%s id=%s",
                                provider,
                                value,
                            )
                            continue
                        if row.get("cover_url"):
                            recovered.append(row)
                            accepted += 1
                    _log(
                        log,
                        "info",
                        "Metadata Engine cover: provider=%s id=%s records=%d with_cover=%d",
                        provider,
                        value,
                        len(found),
                        accepted,
                    )
                except Exception as exc:
                    _log(
                        log,
                        "error",
                        "Metadata Engine cover: provider=%s id=%s lookup failed: %s",
                        provider,
                        value,
                        exc,
                    )
        return recovered

    def _initial_provider_batch(
        self, provider_ids, query, authors, identifiers, timeout, abort
    ):
        """Start providers together, retaining deterministic output and failure isolation."""
        providers = list(dict.fromkeys(provider_ids))
        if not providers or abort.is_set():
            return {}

        def lookup(provider):
            results = {}
            modes = (None, "books") if provider == "itunes" else (None,)
            for mode in modes:
                if abort.is_set():
                    break
                try:
                    kwargs = {"metadata_type": mode} if mode else {}
                    found = self._search(
                        provider, query, authors, identifiers, timeout, **kwargs
                    )
                    results[(provider, mode)] = (found, None)
                except Exception as exc:
                    results[(provider, mode)] = ([], exc)
            return results

        # One worker per provider overlaps the independent initial requests.
        # Alternate modes within a provider remain serial; server-side queues
        # continue to own provider rate limits and cache access.
        with ThreadPoolExecutor(max_workers=len(providers)) as executor:
            futures = [executor.submit(lookup, provider) for provider in providers]
            batches = [future.result() for future in futures]
        return {key: value for batch in batches for key, value in batch.items()}

    def _book_provider_ids(self, log, timeout, abort):
        """Check Google Books first through Metadata Engine's provider endpoint."""
        try:
            selected = self._discover(timeout, abort)
        except Exception as exc:
            _log(log, "error", "Metadata Engine provider discovery failed: %s", exc)
            selected = self._configured_provider_ids()
        return list(dict.fromkeys(["google_books", *selected]))

    def _discover(self, timeout, abort):
        if abort.is_set():
            return []
        payload = self._http_json(self._base_url() + "/plugins", timeout)
        if not isinstance(payload.get("plugins"), list):
            raise ValueError("Metadata Engine API schema: plugins must be an array")
        found = []
        for item in payload["plugins"]:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict) and item.get("enabled", True):
                media_types = item.get("metadata_types")
                if isinstance(media_types, list) and media_types:
                    if not any(
                        self._book_media({"metadata_type": value})
                        for value in media_types
                    ):
                        continue
                pid = item.get("plugin_id") or item.get("id") or item.get("name")
                if pid:
                    found.append(str(pid))
            elif not isinstance(item, dict):
                raise ValueError("Metadata Engine API schema: invalid plugin entry")
        return found

    def _search(
        self, provider_id, query, authors, identifiers, timeout, metadata_type=None
    ):
        params = {
            "query": query,
            "limit": max(
                1, min(100, int(self.prefs.get("per_plugin_limit", 10) or 10))
            ),
        }
        if metadata_type:
            params["metadata_type"] = metadata_type
        if authors:
            params["author"] = ", ".join(authors)
        isbn = self._identifier(identifiers, "isbn", "isbn13", "isbn10")
        asin = self._identifier(identifiers, "asin")
        if (
            not asin
            and provider_id in {"audible", "audnexus"}
            and self._audiobook_cover_lookup(identifiers)
        ):
            asin = self._cover_asin(identifiers)
        language = self._identifier(identifiers, "language", "lang")
        if isbn:
            params["isbn"] = isbn
        # Goodreads supports ISBN, but the engine skips ASIN requests for it.
        if asin and provider_id != "goodreads":
            params["asin"] = asin
        if language:
            params["language"] = language

        url = "%s/plugins/%s/search?%s" % (
            self._base_url(),
            quote(str(provider_id), safe=""),
            urlencode(params),
        )
        data = self._http_json(url, timeout)
        if not isinstance(data.get("results"), list):
            raise ValueError("Metadata Engine API schema: results must be an array")
        out = []
        for row in data["results"]:
            if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                raise ValueError(
                    "Metadata Engine API schema: each result needs a string title"
                )
            if not self._book_media(row):
                continue
            row = dict(row)
            row.setdefault("source", provider_id)
            out.append(row)
        return out

    @classmethod
    def _with_requested_series(cls, row, title, authors, identifiers):
        """Restore series data explicitly supplied in a matching Calibre title."""
        if cls._series_identity(row)[0] or not authors or not row.get("authors"):
            return row
        isbn = cls._canonical_isbn(
            cls._identifier(identifiers, "isbn", "isbn13", "isbn10")
        )
        suffix = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:book\s*|#)(\d+(?:\.\d+)?)\)",
            cls._text(title) or "",
            re.IGNORECASE,
        )
        if suffix:
            book_title, name, position = suffix.groups()
            series = [{"name": name, "position": float(position)}]
            target = {
                "title": book_title,
                "authors": authors,
                "series": series,
                "isbn": isbn,
            }
            asin = cls._cover_asin(identifiers)
            if (
                cls._work_description_matches(target, row)
                and cls._identity_consistent(target, row)
                and cls._compatible_identifiers(dict(target, asin=asin), row)
            ):
                return dict(row, series=series)
            return row
        if not isbn or isbn != cls._canonical_isbn(row.get("isbn")):
            return row
        requested = re.fullmatch(
            r"(.+?)\s+(\d+(?:\.\d+)?):\s+(.+)", cls._text(title) or ""
        )
        returned = re.fullmatch(
            r"(.+?)\s+\(([^()]+)\)", cls._text(row.get("title")) or ""
        )
        if not requested:
            return row
        name, position, book_title = requested.groups()
        if not returned:
            target = {"title": book_title, "authors": authors, "isbn": isbn}
            if cls._work_description_matches(target, row):
                return dict(row, series=[{"name": name, "position": float(position)}])
            return row
        if cls._work_title(name)[0] != cls._work_title(returned[2])[0]:
            return row
        if not cls._work_description_matches(
            {"title": book_title, "authors": authors},
            dict(row, title=returned[1]),
        ):
            return row
        return dict(row, series=[{"name": name, "position": float(position)}])

    @classmethod
    def _usable_description(cls, value):
        """Treat catalog physical details and visually empty HTML as missing."""
        description = cls._text(value)
        if not description:
            return None
        visible = unescape(re.sub(r"<[^<>]+>", " ", description))
        visible = " ".join(visible.split()).rstrip(" :;")
        if re.fullmatch(
            r"(?:\d+|one)\s+(?:v(?:ol)?\.?|volumes?|parts?|audio\s+discs?|discs?)"
            r"(?:\s*\([^)]*\))?(?:\s*[:;]\s*.*)?\.?",
            visible,
            re.IGNORECASE,
        ):
            return None
        if not visible or re.fullmatch(
            r"(?:(?:[ivxlcdm]+|\d+)\s*,\s*)?"
            r"\d+\s*(?:p(?:p)?\.?|pages?|leaves)"
            r"(?:\s*:\s*(?:col\.\s*)?(?:ill\.|illustrations))?"
            r"(?:\s*[:;]\s*\d+(?:[.,]\d+)?\s*cm\.?)?\.?",
            visible,
            re.IGNORECASE,
        ):
            return None
        return description

    @classmethod
    def _with_edition_correction(cls, row):
        """Apply the owner's subject and paragraph choices to the verified edition."""
        isbn = "9781718159075"
        if cls._canonical_isbn(row.get("isbn")) != isbn:
            return row
        expected = {
            "isbn": isbn,
            "title": "Tales from Alternate Earths 2",
            "subtitle": "Eleven New Broadcasts from Parallel Dimensions",
            "authors": ["Daniel M. Bensen"],
            "series": [{"name": "Tales From Alternate Earths", "position": 2}],
        }
        if not row.get("authors") or not cls._identity_consistent(expected, row):
            return row
        corrected = dict(
            row,
            genres=[],
            tags=[
                "Alternate History",
                "Science Fiction",
                "Fantasy",
                "Anthology",
                "Anthologies",
            ],
        )
        description = cls._text(row.get("description")) or ""
        single_paragraph = re.fullmatch(
            r"(?:<div>\s*)?<p>([^<>]*)</p>(?:\s*</div>)?", description, re.IGNORECASE
        )
        if single_paragraph:
            description = single_paragraph[1]
        if not re.search(r"</?[a-z][^<>]*>", description, re.IGNORECASE):
            paragraphs = re.split(
                r"(?<=[.!?])\s+(?=Step into the worlds\b|All this and more\.|"
                r"Authors\s+Jessica Holmes\b)",
                description,
                flags=re.IGNORECASE,
            )
            if len(paragraphs) > 1:
                corrected["description"] = "".join(
                    f"<p>{escape(unescape(paragraph.strip()))}</p>"
                    for paragraph in paragraphs
                )
        return corrected

    @staticmethod
    def _author_credit_names(credit):
        """Split a credited byline, accepting only name-shaped alias pairs."""
        credit = credit.strip().rstrip(".")
        alias = re.fullmatch(
            r"(.+?)\s+(?:writing\s+as\s+(.+)|\(([^()]+)\))", credit, re.IGNORECASE
        )
        if (
            alias
            and alias[3]
            and re.search(
                r"\b(?:author|editor|illustrator|narrator|translator)\b",
                alias[3],
                re.IGNORECASE,
            )
        ):
            return []
        names = [alias[1], alias[2] or alias[3]] if alias else [credit]
        for name in names:
            words = name.split()
            minimum = 1 if alias and alias[2] else 2
            if not minimum <= len(words) <= 8 or not all(
                re.fullmatch(r"[^\W\d_]+(?:[.'’\-][^\W\d_]+)*\.?", word)
                and (
                    word[0].isupper()
                    or word in {"da", "de", "del", "di", "la", "van", "von"}
                )
                for word in words
            ):
                return []
        return names

    @classmethod
    def _description_authors(cls, description, authors):
        """Supplement corroborated author credits and retain explicit pen names."""
        if not description or not authors:
            return authors
        visible = re.sub(r"</(?:p|div|li)>|<br\s*/?>", "\n", description, flags=re.I)
        visible = unescape(re.sub(r"<[^<>]+>", "", visible))
        credits = re.findall(
            r"(?:^|(?<=[.!?])\s+)\s*Authors\s*:\s*([^\n]+)", visible, re.I | re.M
        )
        credits.extend(
            re.findall(
                r"(?:^|(?<=[.!?])\s+)\s*Authors\s+([^\n]{1,600}?)\s+show us\b",
                visible,
                re.I | re.M,
            )
        )
        credits.extend(
            line
            for line in visible.splitlines()
            if "(" in line or " writing as " in line.casefold()
        )
        if not credits:
            return authors
        expanded = []
        for author in authors:
            names = cls._author_credit_names(author)
            expanded.extend(names if len(names) == 2 else [author])
        known = {cls._author_words(name) for name in expanded}
        additional = []
        for credit in credits:
            entries = re.split(
                r"(?:,\s*(?:and\s+)?|\s+(?:and|&)\s+)(?![^()]*\))", credit, flags=re.I
            )
            groups = [cls._author_credit_names(entry) for entry in entries]
            if not all(groups):
                continue
            names = [name for group in groups for name in group]
            if any(
                cls._author_words_match(cls._author_words(name), author)
                for name in names
                for author in known
            ):
                additional.extend(names)
        if not additional:
            return authors
        # Preserve established spellings and the supplied author order.
        unique = {}
        for name in [*expanded, *additional]:
            unique.setdefault(cls._author_words(name), name)
        return list(unique.values())

    @staticmethod
    def _format_review_quotes(description):
        """Set off attributed review blurbs without disturbing provider markup."""
        original = description
        has_html = re.search(r"</?[a-z][^<>]*>", description, re.IGNORECASE)
        if not has_html:
            description = unescape(description)
        quote = re.compile(
            r'(?P<quote>(?:["“]|&quot;|&ldquo;)'
            r'(?:(?!["“”]|&quot;|&ldquo;|&rdquo;|<br\b).)+?'
            r'(?:["”]|&quot;|&rdquo;))'
            r"[ \t]*(?P<dash>[-–—])[ \t]*"
            r'(?P<credit>[A-Z](?:[^\r\n<"“”]|</?(?:i|em|b|strong)>)*?)'
            r'(?=[ \t]*(?:["“]|&quot;|&ldquo;|<br\b|\r?\n|$))',
        )
        if not quote.search(description):
            return original
        # Existing block boundaries already separate their contents. Only
        # insert blank lines within the same paragraph or loose text block.
        blocks = re.split(
            r"(</?(?:p|div|li|blockquote|h[1-6]|ul|ol)\b[^<>]*>)",
            description,
            flags=re.IGNORECASE,
        )
        for index in range(0, len(blocks), 2):
            block = blocks[index]
            matches = list(quote.finditer(block))
            if not matches:
                continue
            pieces = []
            end = 0
            for match in matches:
                pieces.append(block[end : match.start()])
                pieces.append(f"{match['quote']} {match['dash']} {match['credit']}")
                end = match.end()
            pieces.append(block[end:])
            fragments = []
            for piece in pieces:
                piece = re.sub(
                    r"^(?:\s|<br\s*/?>)+|(?:\s|<br\s*/?>)+$",
                    "",
                    piece,
                    flags=re.IGNORECASE,
                )
                if piece:
                    if not has_html:
                        piece = escape(piece).replace("\n", "<br />")
                    fragments.append(piece)
            blocks[index] = "<br /><br />".join(fragments)
        return "".join(blocks)

    @staticmethod
    def _format_description(description):
        """Render clear plain-text lists without rewriting provider HTML or prose."""
        if re.search(r"</?[a-z][^<>]*>", description, re.IGNORECASE):
            return description

        def render_list(kind, items, start=1):
            attribute = f' start="{start}"' if kind == "ol" and start != 1 else ""
            body = "".join(f"<li>{escape(item)}</li>" for item in items)
            return f"<{kind}{attribute}>{body}</{kind}>"

        def contents():
            # Catalog records can concatenate a semicolon-delimited contents
            # field and an abstract. Require consecutive explicit chapter labels.
            entries = [part.strip() for part in description.split(";")]
            front = {"foreword", "preface", "introduction", "prologue"}
            i = 0
            while i < len(entries) and entries[i].rstrip(".").casefold() in front:
                i += 1
            first = i
            chapters = []
            while i < len(entries):
                chapter = re.fullmatch(
                    r"Chapter\s+(\d{1,3}):\s*(.+)", entries[i], re.IGNORECASE
                )
                if not chapter or int(chapter[1]) != len(chapters) + 1:
                    break
                chapters.append(chapter[2])
                i += 1
            if len(chapters) < 2:
                return None
            back = []
            while i < len(entries) and re.fullmatch(
                r"Appendix\s+[A-Z0-9]+:.*|Find out more on\s+\S+|"
                r"Acknowledg(?:e)?ments\.?|Index\.?|Bibliography\.?|Epilogue\.?",
                entries[i],
                re.IGNORECASE,
            ):
                back.append(entries[i].rstrip("."))
                i += 1
            blocks = ["<p><strong>Contents</strong></p>"]
            if first:
                blocks.append(render_list("ul", entries[:first]))
            blocks.append(render_list("ol", chapters))
            if back:
                blocks.append(render_list("ul", back))
            remainder = "; ".join(entries[i:]).strip()
            if remainder:
                blocks.append("<p>" + escape(remainder) + "</p>")
            return "".join(blocks)

        def part_contents():
            # Catalog ISBD contents use spaced double dashes between entries.
            entries = re.split(r"\s+--\s+", description.strip())
            front_labels = {
                "acknowledgements",
                "acknowledgments",
                "foreword",
                "introduction",
                "preface",
                "prologue",
                "table of contents",
                "contents",
            }
            front, parts = [], []
            for entry in entries:
                part = re.fullmatch(
                    r"(?:Pt\.?|Part)\s+(\d{1,3}):\s*(.+)", entry, re.IGNORECASE
                )
                if part:
                    if int(part[1]) != len(parts) + 1:
                        return None
                    heading, separator, first_entry = part[2].partition(": ")
                    parts.append((heading, [first_entry] if separator else []))
                elif parts:
                    parts[-1][1].append(entry)
                elif entry.rstrip(".:").casefold() in front_labels:
                    if entry.rstrip(".:").casefold() not in {
                        "contents",
                        "table of contents",
                    }:
                        front.append(entry)
                else:
                    return None
            if len(parts) < 2 or not all(items for _, items in parts):
                return None

            remainder = ""
            last = parts[-1][1]
            ending = re.fullmatch(
                r"(Index|Bibliography)\.\s+(.+)", last[-1], re.IGNORECASE
            )
            if ending:
                last[-1], remainder = ending.groups()
            blocks = ["<p><strong>Contents</strong></p>"]
            if front:
                blocks.append(render_list("ul", front))
            blocks.append("<ol>")
            for heading, items in parts:
                blocks.append(f"<li>{escape(heading)}{render_list('ul', items)}</li>")
            blocks.append("</ol>")
            if remainder:
                blocks.append(f"<p>{escape(remainder)}</p>")
            return "".join(blocks)

        toc = contents() or part_contents()
        if toc:
            return toc

        lines = description.splitlines()
        blocks, prose = [], []
        formatted = False

        def flush_prose():
            if prose:
                blocks.append("<p>" + "<br>".join(map(escape, prose)) + "</p>")
                prose.clear()

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            numbered = re.fullmatch(r"(\d{1,3})[.)]\s+(.+)", line)
            bullet = re.fullmatch(r"[-*•–—]\s+(.+)", line)
            if numbered or bullet:
                j = i + 1
                items = [numbered[2] if numbered else bullet[1]]
                start = int(numbered[1]) if numbered else 1
                while j < len(lines):
                    candidate = lines[j].strip()
                    if numbered:
                        match = re.fullmatch(r"(\d{1,3})[.)]\s+(.+)", candidate)
                        if not match or int(match[1]) != start + len(items):
                            break
                        items.append(match[2])
                    else:
                        match = re.fullmatch(r"[-*•–—]\s+(.+)", candidate)
                        if not match:
                            break
                        items.append(match[1])
                    j += 1
                if len(items) >= 2:
                    flush_prose()
                    kind = "ol" if numbered else "ul"
                    blocks.append(render_list(kind, items, start))
                    formatted = True
                    i = j
                    continue
            if line:
                prose.append(line)
            else:
                flush_prose()
            i += 1
        flush_prose()
        return "".join(blocks) if formatted else description

    def _to_metadata(self, row, relevance):
        if not self._book_media(row):
            return None
        title = self._text(row.get("title"))
        if not title:
            return None
        # Some books carry their series only in a trailing "Series Book N".
        # Keep a structured provider series authoritative when available.
        inferred_series = None
        prefix = self._numbered_title_prefix(title, row.get("subtitle"))
        if prefix and row.get("series"):
            name, position = self._series_identity(row)
            if name == prefix[0].casefold() and position == float(prefix[1]):
                title = prefix[2]
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:Book\s*|#)(\d+(?:\.\d+)?)\)", title, re.IGNORECASE
        )
        if match:
            series_name = match.group(2).rstrip(" ,")
            # A numbered franchise prefix (e.g. "Aeon 14:") is not the series.
            series_name = re.sub(r"^.+?\s+\d+\s*:\s*", "", series_name)
            if series_name:
                title = match.group(1).strip()
                inferred_series = (series_name, float(match.group(3)))
        subtitle = self._text(row.get("subtitle"))
        if subtitle and subtitle.casefold() not in title.casefold():
            title = "%s: %s" % (title, subtitle)

        authors = [
            self._canonical_author_name(name) for name in self._list(row.get("authors"))
        ] or ["Unknown"]
        mi = Metadata(title, authors)
        mi.source_relevance = relevance

        if row.get("publisher"):
            mi.publisher = self._text(row["publisher"])
        description = self._usable_description(row.get("description"))
        inline_warnings = []
        if description:

            def collect_warnings(match):
                inline_warnings.extend(re.split(r"[,;]", match[1]))
                return ""

            description = re.sub(
                r"\((?:TW\s*/\s*CW|TW|CW|trigger warnings?|content warnings?)"
                r"\s*:\s*([^()]+)\)",
                collect_warnings,
                description,
                flags=re.IGNORECASE,
            ).strip()
            # Remove explicit source-attribution lines, not names in prose.
            description = re.sub(
                r"(?im)^\s*(?:metadata )?source\s*:\s*(?:amazon(?:\.com)?|audible|goodreads|hardcover|itunes|apple books|open library|google books|isbnlab|asintojson)\s*$",
                "",
                description,
            ).strip()
            if self._usable_description(description):
                mi.comments = self._format_review_quotes(
                    self._format_description(description)
                )
        if row.get("language"):
            mi.languages = [self._text(row["language"])]

        # Provider names, shop navigation and format labels are not subjects.
        source = (self._text(row.get("source")) or "").casefold()
        excluded = {
            "amazon",
            "amazon.com",
            "audible",
            "books",
            "work",
            "not mature",
            "mature",
            "audiobook",
            "audiobooks",
            "goodreads",
            "hardcover",
            "itunes",
            "apple books",
            "open library",
            "internet archive",
            "archive.org",
            "internetarchivebooks",
            "inlibrary",
            "printdisabled",
            "library of congress",
            "sru",
            "z39.50",
            "google books",
            "storygraph",
            "trigger warning database",
            "content warnings",
            "isbnlab",
            "asintojson",
            "cleaned",
            "kindle store",
            "kindle ebooks",
            "kindle e-books",
        }
        excluded.add(source)

        def normalize_label(value):
            return re.sub(r"[-_\s]+", " ", value.casefold()).strip()

        excluded = {normalize_label(value) for value in excluded}
        tags = self._list(row.get("genres")) + self._list(row.get("tags"))
        unique_tags = []
        seen_tags = set()
        # Split catalog subject paths before normalizing individual tags.
        subjects = (
            part
            for tag in tags
            if not re.match(r"https?://", tag, re.IGNORECASE)
            for part in tag.split("/")
        )
        for tag in subjects:
            tag = tag.strip().rstrip(".").rstrip()
            if not tag:
                continue
            if normalize_label(tag) in excluded or tag.casefold().strip().startswith(
                "metadata-engine:"
            ):
                continue
            identity = " ".join(unicodedata.normalize("NFKC", tag).casefold().split())
            if identity not in seen_tags:
                seen_tags.add(identity)
                unique_tags.append(tag)
        mi.tags = unique_tags

        if row.get("isbn") and self._canonical_isbn(row["isbn"]):
            mi.set_identifier("isbn", self._text(row["isbn"]))
        if row.get("asin"):
            mi.set_identifier("asin", self._text(row["asin"]))
        if self._cover_media(row) == "audiobook":
            mi.set_identifier("metadata_engine_media", "audiobook")

        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        for scheme, keys in (
            ("goodreads", ("goodreads_id",)),
            ("lccn", ("lccn",)),
            ("oclc", ("oclc", "oclc_numbers")),
            ("dewey", ("dewey_decimal", "dewey_decimal_class")),
            ("openlibrary", ("open_library_edition_id", "open_library_work_id")),
        ):
            value = next(
                (
                    self._list(extra.get(key))[0]
                    for key in keys
                    if self._list(extra.get(key))
                ),
                None,
            )
            if value:
                mi.set_identifier(scheme, value)

        source = self._text(row.get("source"))
        source_id = self._text(row.get("source_id"))
        if source and source_id:
            mi.set_identifier(
                "metadata_engine_%s" % source.lower().replace(" ", "_"), source_id
            )

        year = self._text(row.get("published_year"))
        if year:
            try:
                mi.pubdate = datetime(int(year[:4]), 1, 1)
            except Exception:
                pass

        series = row.get("series")
        if series:
            item = series[0] if isinstance(series, list) else series
            if isinstance(item, str):
                mi.series = item
            elif isinstance(item, dict):
                mi.series = self._text(
                    item.get("name") or item.get("title") or item.get("series")
                )
                idx = item.get("index", item.get("position", item.get("number")))
                try:
                    mi.series_index = float(idx) if idx is not None else None
                except Exception:
                    pass

        if not mi.series:
            inferred_series = inferred_series or self._numbered_volume(row)
            if inferred_series is None:
                volume = re.fullmatch(
                    r"(.+?)(?:\s*[:,–—-]\s*|\s+)Volume\s+(\d+(?:\.\d+)?)",
                    title,
                    re.IGNORECASE,
                )
                if volume:
                    inferred_series = (volume[1].strip(), float(volume[2]))
            if inferred_series:
                mi.series, mi.series_index = inferred_series

        if row.get("cover_url"):
            mi.has_cached_cover = True
            self._remember_cover(row)

        extras = []
        narrators = self._list(row.get("narrators"))
        audiobook = self._cover_media(row) == "audiobook"
        if audiobook and narrators:
            extras.append("Narrator(s): " + ", ".join(narrators))
        if audiobook and row.get("duration_seconds"):
            try:
                seconds = int(row["duration_seconds"])
                hours, rem = divmod(seconds, 3600)
                minutes = rem // 60
                extras.append("Duration: %dh %02dm" % (hours, minutes))
            except Exception:
                pass
        warnings = self._list(row.get("parental_warnings"))
        existing = (mi.comments or "").casefold()
        missing = [value for value in warnings if value.casefold() not in existing]
        if missing:
            extras.append("Parental warnings: " + "; ".join(dict.fromkeys(missing)))
        if extras:
            mi.comments = (mi.comments or "") + "\n\n" + "\n".join(extras)
        mi.comments = self._with_trigger_warnings(
            mi.comments,
            self._list(row.get("trigger_warnings"))
            + self._list(row.get("content_warnings"))
            + inline_warnings,
        )
        return mi

    @classmethod
    def _with_trigger_warnings(cls, comments, warnings):
        """Prefix verified warnings as a separate paragraph in either comment format."""
        if not warnings:
            return comments
        body = (comments or "").lstrip()
        referral = cls._warning_referral(body)
        if referral:
            body = (body[: referral.start()] + body[referral.end() :]).strip()
        is_html = body.startswith("<")
        if is_html:
            pattern = r"<p>\s*trigger warnings:\s*([^<]+)</p>\s*"
        else:
            pattern = r"trigger warnings:[ \t]*([^\r\n]+)(?:\r?\n|$)\s*"
        header = re.match(pattern, body, re.IGNORECASE)
        if header:
            warnings = re.split(r"[,;]", unescape(header[1])) + warnings
            body = body[header.end() :]
        unique = {}
        for warning in warnings:
            label = warning.strip()
            for obfuscated, normalized in (
                ("vio-lence", "violence"),
                ("go-re", "gore"),
                ("blo-od", "blood"),
                ("se-x-ual", "sexual"),
                ("bond-age", "bondage"),
                ("c-nc", "CNC"),
            ):
                label = re.sub(
                    re.escape(obfuscated), normalized, label, flags=re.IGNORECASE
                )
            if label and not cls._warning_referral(label):
                unique.setdefault(label.casefold(), label)
        if not unique:
            return comments
        line = "trigger warnings: " + ", ".join(unique.values())
        if is_html:
            return f"<p>{escape(line)}</p>{body}"
        return f"{line}\n\n{body}" if body else line

    def _remember_cover(self, row):
        url = self._text(row.get("cover_url"))
        if not url:
            return
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        source = self._text(row.get("source"))
        source_id = self._text(row.get("source_id"))
        if source and source_id:
            self._cover_urls[
                ("metadata_engine_" + source.lower().replace(" ", "_"), source_id)
            ] = url
        goodreads_id = self._text(extra.get("goodreads_id"))
        if not goodreads_id and source == "goodreads":
            goodreads_id = source_id
        if goodreads_id and re.fullmatch(r"\d{1,16}", goodreads_id):
            self._cover_urls[("goodreads", goodreads_id)] = url
        for key in ("isbn", "asin", "source_id"):
            value = self._text(row.get(key))
            if value:
                self._cover_urls[(key, value)] = url

    def _cached_cover(self, identifiers):
        for key, value in identifiers.items():
            key = str(key).lower()
            url = self._cover_urls.get(
                ("asin" if key == "mobi-asin" else key, str(value))
            )
            if url:
                return url
        return None

    def _http_json(self, url, timeout):
        payload = json.loads(self._http_bytes(url, timeout).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Metadata Engine API schema: response must be an object")
        return payload

    @staticmethod
    def _book_media(row):
        """Ignore explicit non-book media while accepting legacy untyped books."""
        value = row.get("metadata_type")
        if value is None or value == "":
            return True
        if not isinstance(value, str):
            raise ValueError(
                "Metadata Engine API schema: metadata_type must be a string"
            )
        return value.strip().lower().replace("_", " ").replace("-", " ") in {
            "book",
            "books",
            "ebook",
            "ebooks",
            "audiobook",
            "audiobooks",
            "comic",
            "comics",
            "manga",
            "graphic novel",
            "graphic novels",
        }

    def _http_bytes(self, url, timeout):
        return self._http_response(url, timeout)[0]

    def _http_response(self, url, timeout):
        """Return the body and final URL after following redirects."""
        headers = {
            "Accept": "application/json, image/*;q=0.9, */*;q=0.1",
            "User-Agent": "Calibre-Metadata-Engine/1.1.68",
            "Accept-Encoding": "identity",
        }
        token = str(self.prefs.get("api_token", "") or "").strip()
        req = Request(url, headers=headers)
        if token and urlsplit(url)[:2] == urlsplit(self._base_url())[:2]:
            req.add_unredirected_header("Authorization", "Bearer " + token)
        context = None
        if url.lower().startswith("https://") and not self.prefs.get(
            "verify_ssl", True
        ):
            context = ssl._create_unverified_context()
        deadline = time.monotonic() + max(1, float(timeout))
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Metadata Engine request retry budget exhausted")
            try:
                with urlopen(
                    req, timeout=remaining / (2 - attempt), context=context
                ) as response:
                    return response.read(), response.geturl()
            except HTTPError as exc:
                if (
                    attempt == 0
                    and exc.code == 503
                    and urlsplit(url).hostname == "www.goodreads.com"
                ):
                    exc.close()
                    continue
                detail = exc.read().decode("utf-8", "replace")
                raise RuntimeError("HTTP %s: %s" % (exc.code, detail or exc.reason))
            except (TimeoutError, URLError) as exc:
                reason = getattr(exc, "reason", exc)
                if attempt == 0 and isinstance(reason, TimeoutError):
                    continue
                raise RuntimeError(str(reason)) from exc

    def _base_url(self):
        return str(self.prefs.get("base_url", "") or "").strip().rstrip("/")

    def _configured_provider_ids(self):
        return [
            x.strip()
            for x in str(self.prefs.get("plugin_ids", "") or "").split(",")
            if x.strip()
        ]

    @classmethod
    def _cover_identifier_score(cls, row, identifiers):
        """Prefer an exact edition identifier before generic confidence ranking."""
        wanted_isbn = cls._canonical_isbn(
            cls._identifier(identifiers, "isbn", "isbn13", "isbn10")
        )
        row_isbn = cls._canonical_isbn(row.get("isbn"))
        wanted_asin = cls._cover_asin(identifiers)
        row_asin = cls._text(row.get("asin"))
        if wanted_asin and row_asin and wanted_asin.upper() != row_asin.upper():
            return 0
        if wanted_isbn and row_isbn:
            return 3 if wanted_isbn == row_isbn else 0
        if wanted_asin and row_asin:
            return 2

        wanted_goodreads = cls._identifier(
            identifiers, "goodreads", "metadata_engine_goodreads"
        )
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        row_goodreads = cls._text(extra.get("goodreads_id"))
        if not row_goodreads and row.get("source") == "goodreads":
            row_goodreads = cls._text(row.get("source_id"))
        if wanted_goodreads and row_goodreads:
            return 2 if wanted_goodreads == row_goodreads else 0

        if row.get("native_google_cover") and row.get("source_id") == cls._identifier(
            identifiers, "metadata_engine_google_books", "google"
        ):
            return 2

        return 1

    @staticmethod
    def _looks_like_image(data):
        if not data or len(data) < 12:
            return False
        return (
            data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )

    @staticmethod
    def _identifier(identifiers, *keys):
        lower = {str(k).lower(): v for k, v in identifiers.items()}
        for key in keys:
            if key == "asin" and not lower.get(key):
                key = "mobi-asin"
            if lower.get(key):
                return str(lower[key]).strip()
        return None

    @classmethod
    def _make_query(cls, title, authors, identifiers):
        """Keep the title clean; author and edition IDs travel as API parameters."""
        if title and str(title).strip():
            return cls._search_title(title)
        for key in ("isbn", "isbn13", "isbn10", "asin"):
            value = cls._identifier(identifiers, key)
            if value:
                return value
        parts = [str(a).strip() for a in (authors or []) if str(a).strip()]
        return " ".join(parts or [str(v).strip() for v in identifiers.values() if v])

    @staticmethod
    def _text(value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @classmethod
    def _list(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [x for x in (cls._text(v) for v in value) if x]
        value = cls._text(value)
        return [value] if value else []

    @staticmethod
    def _restore_title_article(title):
        """Convert an English catalog sort title to reading order."""
        match = re.fullmatch(r"(.+),\s*(the|an|a)", title, re.IGNORECASE)
        return f"{match[2].capitalize()} {match[1]}" if match else title

    @classmethod
    def _search_title(cls, value, subtitle=None):
        """Extract the work title for search without changing its case."""
        title = cls._restore_title_article(cls._text(value) or "")
        indexed = cls._catalog_index_title(title)
        if indexed:
            title = indexed[1]
        prefix = cls._numbered_title_prefix(title, subtitle)
        if prefix:
            title = prefix[2]
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:book\s*|#)(\d+(?:\.\d+)?)\)",
            title,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else title

    @staticmethod
    def _catalog_index_title(value):
        """Extract franchise and work hints from a title with a middle index."""
        match = re.fullmatch(
            r"(.+?)\s+[-–—]\s+[0-9]+(?:\.[0-9]+)?\s+[-–—]\s+(\S.*)",
            value or "",
        )
        return match.groups() if match else None

    @classmethod
    def _indexed_title_matches(cls, indexed, catalog, franchise, work):
        """Verify a stripped search hint against edition or catalog evidence."""
        if not cls._list(indexed.get("authors")) or not cls._list(
            catalog.get("authors")
        ):
            return False
        expected_isbn = cls._canonical_isbn(indexed.get("isbn"))
        if expected_isbn and expected_isbn != cls._canonical_isbn(catalog.get("isbn")):
            return False
        base = cls._work_title(work)[0]
        title = cls._work_title(catalog.get("title"))[0]
        subtitle = cls._work_title(catalog.get("subtitle"))[0]

        def franchise_label(value):
            return cls._franchise_label_matches(franchise, value)

        if title == base:
            if subtitle:
                return franchise_label(subtitle)
            return bool(expected_isbn) or franchise_label(
                cls._series_identity(catalog)[0]
            )
        labels = re.split(r":\s+|\s+[-–—]\s+", title, maxsplit=1)
        if len(labels) == 2:
            first, second = labels
            if (first == base and franchise_label(second)) or (
                second == base and franchise_label(first)
            ):
                return True
        parenthetical = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", title)
        return bool(
            parenthetical
            and parenthetical[1] == base
            and franchise_label(parenthetical[2])
        )

    @staticmethod
    def _franchise_label_matches(franchise, label):
        """Recognize catalog extensions of a franchise name at a word boundary."""
        prefix, value = (
            re.sub(r"[^\w]+", " ", text.casefold()).strip()
            for text in (franchise, label)
        )
        return bool(prefix and (value == prefix or value.startswith(prefix + " ")))

    @classmethod
    def _numbered_title_prefix(cls, value, subtitle=None):
        """Parse a possible series prefix; matching must corroborate its meaning."""
        value = cls._text(value) or ""
        if ":" not in value and cls._text(subtitle):
            value += ": " + cls._text(subtitle)
        match = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?)\s*:\s*(\S.*)", value)
        return match.groups() if match else None

    @classmethod
    def _numbered_volume(cls, row):
        """Recognize a numbered heading when its synopsis names the preceding work."""
        prefix = cls._numbered_title_prefix(row.get("title"), row.get("subtitle"))
        if not prefix or float(prefix[1]) <= 1:
            return None
        name, position, _ = prefix
        description = unescape(
            re.sub(r"<[^<>]+>", " ", cls._text(row.get("description")) or "")
        )
        match = re.search(
            r"\b(?:sequel|successor|follow[- ]up)\s+to\s+[^.!?]{0,80}?"
            rf"\b({re.escape(name)})(?!\w|\s+\d)",
            " ".join(description.split()),
            re.IGNORECASE,
        )
        return (match[1], float(position)) if match else None

    @classmethod
    def _work_title(cls, value):
        """Extract a numbered series suffix only for matching book works."""
        title = cls._restore_title_article(
            unicodedata.normalize("NFKC", cls._text(value) or "")
        ).casefold()
        title = title.translate(str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"}))
        title = " ".join(title.split())
        title = re.sub(r"\s+:\s*", ": ", title)
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:book\s*|#)(\d+(?:\.\d+)?)\)",
            title,
            re.IGNORECASE,
        )
        if not match:
            return title, None, None
        series = re.sub(r"^.+?\s+\d+\s*:\s*", "", match.group(2)).rstrip(" ,")
        return match.group(1).strip(), series, float(match.group(3))

    @classmethod
    def _series_identity(cls, row):
        """Normalize structured series, falling back to a numbered title suffix."""
        title_name, title_series, title_number = cls._work_title(row.get("title"))
        del title_name
        raw = row.get("series")
        item = (raw[0] if raw else None) if isinstance(raw, list) else raw
        if isinstance(item, dict):
            name = cls._text(
                item.get("name") or item.get("title") or item.get("series")
            )
            number = item.get("index", item.get("position", item.get("number")))
            try:
                number = float(number) if number is not None else None
            except (TypeError, ValueError):
                number = None
        else:
            name, number = cls._text(item), None
        name = (name or title_series or "").casefold().strip()
        name = re.sub(r"^.+?\s+\d+\s*:\s*", "", name)
        if not name:
            return "", None
        return name, number if number is not None else title_number

    @staticmethod
    def _series_name_key(value):
        """Normalize optional catalog series labels without altering displayed names."""
        return re.sub(r"\s+(?:trilogy|series)$", "", value or "").strip()

    @classmethod
    def _series_work_key(cls, row):
        """Require an explicit series and position before matching shortened titles."""
        title, _, _ = cls._work_title(row.get("title"))
        numbers = {"one": "1", "two": "2", "three": "3", "four": "4"}
        title = re.sub(
            r"\bterm\s+(one|two|three|four)\b",
            lambda match: "term " + numbers[match.group(1)],
            title,
        )
        series, position = cls._series_identity(row)
        patterns = (
            r"^(.+?):\s*(.+?)\s+-\s+term\s+(\d+(?:\.\d+)?)$",
            r"^(.+?):\s*(.+?),\s*book\s+(\d+(?:\.\d+)?)"
            r"(?:\s*\(unabridged\))?$",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, title)
            if not match:
                continue
            base, named_series, number = match.groups()
            number = float(number)
            if series and cls._series_name_key(series) != cls._series_name_key(
                named_series
            ):
                return None
            if position is not None and position != number:
                return None
            title, series, position = base, named_series, number
            break
        else:
            term = re.fullmatch(r"(.+?):\s*term\s+(\d+(?:\.\d+)?)", title)
            if term:
                if position is None or position != float(term.group(2)):
                    return None
                title = term.group(1)
        if not title or not series or position is None:
            return None
        return title, cls._series_name_key(series), position

    @classmethod
    def _edition_title_key(cls, title):
        """Separate terminal edition decorations while retaining real subtitles."""
        head, separator, subtitle = title.partition(": ")
        words = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
        }
        ordinal = r"\d+(?:st|nd|rd|th)?|" + "|".join(words)
        match = re.fullmatch(
            r"(.+?)(?:,\s*|\s*[-(]\s*|\s+)"
            r"(?:(?:updated|expanded|revised|new|illustrated)\s+(?:and\s+)?)*"
            r"(" + ordinal + r")\s+(?:edition|ed\.?)\)?",
            head,
        )
        number = None
        if match:
            head = match[1].strip(" ,-(")
            token = match[2]
            number = words.get(token)
            if number is None:
                number = int(re.match(r"\d+", token)[0])
        key = head + (separator + subtitle if separator else "")
        return re.sub(r"\s*&\s*", " and ", key), number

    @classmethod
    def _work_titles_compatible(cls, left, right):
        """Compare full titles and corroborated catalog series variants."""
        left_title, _, _ = cls._work_title(left.get("title"))
        right_title, _, _ = cls._work_title(right.get("title"))
        if not left_title or not right_title:
            return False
        for decorated, catalog in ((left, right), (right, left)):
            indexed = cls._catalog_index_title(cls._text(decorated.get("title")))
            if indexed and cls._indexed_title_matches(decorated, catalog, *indexed):
                return True
        left_isbn = cls._canonical_isbn(left.get("isbn"))
        right_isbn = cls._canonical_isbn(right.get("isbn"))
        left_prefix = cls._numbered_title_prefix(
            left.get("title"), left.get("subtitle")
        )
        right_prefix = cls._numbered_title_prefix(
            right.get("title"), right.get("subtitle")
        )
        if (
            left_prefix
            and right_prefix
            and (
                cls._series_name_key(left_prefix[0].casefold())
                != cls._series_name_key(right_prefix[0].casefold())
                or float(left_prefix[1]) != float(right_prefix[1])
            )
        ):
            return False
        for decorated, plain in ((left, right), (right, left)):
            prefix = cls._numbered_title_prefix(
                decorated.get("title"), decorated.get("subtitle")
            )
            if not prefix:
                continue
            name, number, base = prefix
            name = cls._series_name_key(name.casefold())
            evidence = [cls._series_identity(row) for row in (decorated, plain)]
            if any(
                series
                and (
                    cls._series_name_key(series) != name
                    or (position is not None and position != float(number))
                )
                for series, position in evidence
            ):
                continue
            corroborated = bool(left_isbn and left_isbn == right_isbn) or any(
                cls._series_name_key(series) == name and position == float(number)
                for series, position in evidence
            )
            normalized = dict(decorated, title=base)
            if not cls._numbered_title_prefix(decorated.get("title")):
                normalized["subtitle"] = None
            if corroborated and cls._work_titles_compatible(normalized, plain):
                return True
        if left_isbn and left_isbn == right_isbn:
            left_title, left_edition = cls._edition_title_key(left_title)
            right_title, right_edition = cls._edition_title_key(right_title)
            if left_edition and right_edition and left_edition != right_edition:
                return False
        left_asin = cls._cover_asin(left)
        if re.fullmatch(r"B[A-Z0-9]{9}", left_asin) and left_asin == cls._cover_asin(
            right
        ):
            left_base, _, left_sub = left_title.partition(": ")
            right_base, _, right_sub = right_title.partition(": ")
            left_sub = left_sub or cls._work_title(left.get("subtitle"))[0]
            right_sub = right_sub or cls._work_title(right.get("subtitle"))[0]
            if left_base == right_base and not (left_sub and right_sub):
                return True
        if left_isbn and left_isbn == right_isbn and left_title != right_title:
            a, _, a_sub = left_title.partition(": ")
            b, _, b_sub = right_title.partition(": ")
            a_sub = a_sub or cls._work_title(left.get("subtitle"))[0]
            b_sub = b_sub or cls._work_title(right.get("subtitle"))[0]
            if a == b and not (a_sub and b_sub and a_sub != b_sub):
                return True
        if left_title == right_title:
            left_subtitle = cls._work_title(left.get("subtitle"))[0]
            right_subtitle = cls._work_title(right.get("subtitle"))[0]
            return (
                not (left_subtitle and right_subtitle)
                or left_subtitle == right_subtitle
            )
        for base, full, row in (
            (left_title, right_title, left),
            (right_title, left_title, right),
        ):
            subtitle = cls._work_title(row.get("subtitle"))[0]
            if subtitle and f"{base}: {subtitle}" == full:
                return True
        for decorated, plain, witness in (
            (left_title, right_title, right),
            (right_title, left_title, left),
        ):
            suffix = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", decorated)
            series, _ = cls._series_identity(witness)
            if (
                suffix
                and suffix[1] == plain
                and series
                and cls._series_name_key(suffix[2]) == cls._series_name_key(series)
            ):
                return True
        left_key = cls._series_work_key(left)
        right_key = cls._series_work_key(right)
        if left_key is not None and left_key == right_key:
            return True

        def normalize_term_number(title):
            numbers = {"one": "1", "two": "2", "three": "3", "four": "4"}
            return re.sub(
                r"\bterm\s+(one|two|three|four)\b",
                lambda match: "term " + numbers[match.group(1)],
                title,
            )

        left_title = normalize_term_number(left_title)
        right_title = normalize_term_number(right_title)
        # Apple audiobook titles encode the series in a different explicit form.
        # Only accept the form when both the series and position agree.
        term_pattern = r"^(.+?):\s*(.+?)\s+-\s+term\s+(\d+(?:\.\d+)?)$"
        audio_pattern = (
            r"^(.+?):\s*(.+?),\s*book\s+(\d+(?:\.\d+)?)"
            r"(?:\s*\(unabridged\))?$"
        )
        for term_title, audio_title in (
            (left_title, right_title),
            (right_title, left_title),
        ):
            term = re.fullmatch(term_pattern, term_title)
            audio = re.fullmatch(audio_pattern, audio_title)
            if term and audio and term.groups() == audio.groups():
                return True
        for long_title, short_title, series_row in (
            (left_title, right_title, right),
            (right_title, left_title, left),
        ):
            long_parts = long_title.split(": ", 1)
            short_parts = short_title.split(": ", 1)
            if (
                len(long_parts) != 2
                or len(short_parts) != 2
                or long_parts[0] != short_parts[0]
            ):
                continue
            marker = " - " + short_parts[1]
            if not long_parts[1].endswith(marker):
                continue
            inserted = long_parts[1][: -len(marker)].strip()
            series, _ = cls._series_identity(series_row)
            series = re.sub(r"\s+(?:trilogy|series)$", "", series).strip()
            if inserted and series and inserted == series:
                return True
        return False

    @classmethod
    def _work_description_matches(cls, winner, donor):
        """Require matching work title, compatible contributors and series."""
        _, left_series, left_number = cls._work_title(winner.get("title"))
        _, right_series, right_number = cls._work_title(donor.get("title"))
        if not cls._work_titles_compatible(winner, donor):
            return False
        if (
            left_series
            and right_series
            and (
                cls._series_name_key(left_series) != cls._series_name_key(right_series)
                or left_number != right_number
            )
        ):
            return False
        left_series, left_number = cls._series_identity(winner)
        right_series, right_number = cls._series_identity(donor)
        if (
            left_series
            and right_series
            and cls._series_name_key(left_series) != cls._series_name_key(right_series)
        ):
            return False
        if (
            left_number is not None
            and right_number is not None
            and left_number != right_number
        ):
            return False

        def names(row):
            people = cls._list(row.get("authors"))
            for contributor in row.get("contributors") or []:
                if not isinstance(contributor, dict):
                    continue
                role = str(contributor.get("role") or "").casefold().strip()
                if role in {"author", "writer", "editor", "edited by", "co-author"}:
                    name = cls._text(contributor.get("name"))
                    if name:
                        people.append(name)

            return {
                value for value in (cls._author_words(name) for name in people) if value
            }

        left_people, right_people = names(winner), names(donor)

        def subset(left, right):
            return len(left) <= len(right) and all(
                any(cls._author_words_match(a, b) for b in right) for a in left
            )

        return bool(
            left_people
            and right_people
            and (subset(left_people, right_people) or subset(right_people, left_people))
        )

    @classmethod
    def _canonical_isbn(cls, value):
        """Return a validated ISBN-13 identity, or an empty string for invalid ISBNs."""
        raw = re.sub(r"[\s-]+", "", cls._text(value) or "").upper()
        if re.fullmatch(r"\d{9}[\dX]", raw):
            digits = [10 if char == "X" else int(char) for char in raw]
            if sum((10 - index) * digit for index, digit in enumerate(digits)) % 11:
                return ""
            stem = "978" + raw[:9]
            check = (
                -sum(
                    int(char) * (1 if index % 2 == 0 else 3)
                    for index, char in enumerate(stem)
                )
            ) % 10
            return stem + str(check)
        if re.fullmatch(r"97[89]\d{10}", raw):
            if (
                sum(
                    int(char) * (1 if index % 2 == 0 else 3)
                    for index, char in enumerate(raw)
                )
                % 10
                == 0
            ):
                return raw
        return ""

    @classmethod
    def _compatible_identifiers(cls, left, right):
        """Reject explicitly conflicting edition identifiers."""
        for key in ("isbn", "asin"):
            a, b = cls._text(left.get(key)), cls._text(right.get(key))
            if a and b:
                left_id = (
                    cls._canonical_isbn(a)
                    if key == "isbn"
                    else re.sub(r"[\s-]+", "", a).upper()
                )
                right_id = (
                    cls._canonical_isbn(b)
                    if key == "isbn"
                    else re.sub(r"[\s-]+", "", b).upper()
                )
                if left_id and right_id and left_id != right_id:
                    return False
        left_extra = left.get("extra") if isinstance(left.get("extra"), dict) else {}
        right_extra = right.get("extra") if isinstance(right.get("extra"), dict) else {}
        for keys in (
            ("lccn",),
            ("oclc", "oclc_numbers"),
            ("goodreads_id",),
            ("open_library_edition_id",),
            ("open_library_work_id",),
        ):
            values = []
            for extra in (left_extra, right_extra):
                ids = set()
                for key in keys:
                    ids.update(
                        re.sub(r"[\s-]+", "", item).upper()
                        for item in cls._list(extra.get(key))
                    )
                values.append(ids)
            if values[0] and values[1] and not values[0].intersection(values[1]):
                return False
        return True

    @classmethod
    def _edition_evidence_conflicts(cls, left, right):
        """Detect strong, corroborated contradictions for the same known format."""

        def evidence(row):
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            raw_format = str(
                extra.get("physical_format") or extra.get("format") or ""
            ).casefold()
            if "audio" in raw_format:
                kind = "audio"
            elif any(word in raw_format for word in ("kindle", "ebook", "e-book")):
                kind = "ebook"
            elif any(
                word in raw_format
                for word in ("hardcover", "hardback", "paperback", "print")
            ):
                kind = "print"
            else:
                kind = ""
            try:
                pages = int(extra.get("page_count") or row.get("page_count") or 0)
                year = int(str(row.get("published_year") or "")[:4])
            except (TypeError, ValueError):
                return kind, 0, 0
            return kind, pages, year

        left_kind, left_pages, left_year = evidence(left)
        right_kind, right_pages, right_year = evidence(right)
        if (
            not left_kind
            or left_kind != right_kind
            or not all((left_pages, right_pages, left_year, right_year))
        ):
            return False
        # One field can legitimately vary between providers. Require two large
        # disagreements before treating the edition evidence as contradictory.
        return abs(left_year - right_year) > 5 and abs(left_pages - right_pages) > max(
            100, 0.3 * max(left_pages, right_pages)
        )

    @classmethod
    def _identity_consistent(cls, left, right):
        """Reject contradictory titles or known contributor sets even with a shared ID."""
        left_title, _, _ = cls._work_title(left.get("title"))
        right_title, _, _ = cls._work_title(right.get("title"))
        if left_title and right_title and not cls._work_titles_compatible(left, right):
            return False
        if cls._edition_evidence_conflicts(left, right):
            return False
        left_series, left_number = cls._series_identity(left)
        right_series, right_number = cls._series_identity(right)
        if (
            left_series
            and right_series
            and cls._series_name_key(left_series) != cls._series_name_key(right_series)
        ):
            return False
        if (
            left_number is not None
            and right_number is not None
            and left_number != right_number
        ):
            return False
        if cls._list(left.get("authors")) and cls._list(right.get("authors")):
            if not cls._work_description_matches(left, right):
                return False
        return True

    @classmethod
    def _same_edition(cls, left, right):
        """Accept a shared edition identifier or the same provider record."""
        if not cls._compatible_identifiers(left, right) or not cls._identity_consistent(
            left, right
        ):
            return False
        for key in ("isbn", "asin"):
            a, b = cls._text(left.get(key)), cls._text(right.get(key))
            if key == "isbn":
                a, b = cls._canonical_isbn(a), cls._canonical_isbn(b)
            else:
                a = re.sub(r"[\s-]+", "", a or "").upper()
                b = re.sub(r"[\s-]+", "", b or "").upper()
            if a and b and a == b:
                return True
        left_extra = left.get("extra") if isinstance(left.get("extra"), dict) else {}
        right_extra = right.get("extra") if isinstance(right.get("extra"), dict) else {}
        for keys in (
            ("lccn",),
            ("oclc", "oclc_numbers"),
            ("goodreads_id",),
            ("open_library_edition_id",),
        ):
            values = []
            for extra in (left_extra, right_extra):
                identifiers = set()
                for key in keys:
                    identifiers.update(
                        re.sub(r"[\s-]+", "", item).upper()
                        for item in cls._list(extra.get(key))
                    )
                values.append(identifiers)
            if values[0].intersection(values[1]):
                return True
        return bool(
            cls._text(left.get("source"))
            and cls._text(left.get("source_id"))
            and str(left["source"]).casefold()
            == str(right.get("source") or "").casefold()
            and str(left["source_id"]).casefold()
            == str(right.get("source_id") or "").casefold()
        )

    @staticmethod
    def _fuller_description(current, candidate):
        """Prefer substantial same-edition prose over a very short blurb."""

        def visible(value):
            return " ".join(unescape(re.sub(r"<[^<>]+>", " ", value)).split())

        previous = visible(current)
        proposed = visible(candidate)
        words = re.findall(r"[^\W_]+", proposed.casefold())
        sentences = re.findall(r"[^\W\d_][.!?](?:\s|$)", proposed)
        return (
            len(previous.split()) < 40
            and len(words) >= 60
            and len(set(words)) >= 35
            and len(sentences) >= 2
        )

    @classmethod
    def _merge_metadata(cls, winner, donor):
        """Fill missing work or edition fields from a compatible provider result."""
        if not cls._compatible_identifiers(
            winner, donor
        ) or not cls._identity_consistent(winner, donor):
            return False
        edition = cls._same_edition(winner, donor)
        work = cls._work_description_matches(winner, donor)
        if not edition and not work:
            return False

        current = cls._usable_description(winner.get("description"))
        description = cls._usable_description(donor.get("description"))
        if description and (
            not current or (edition and cls._fuller_description(current, description))
        ):
            winner["description"] = description
        series = winner.get("series")
        if not series or (isinstance(series, str) and not series.strip()):
            value = donor.get("series")
            if value and (not isinstance(value, str) or value.strip()):
                winner["series"] = value
        # Merge work-level subjects and warning labels from compatible records.
        for field in (
            "genres",
            "tags",
            "trigger_warnings",
            "content_warnings",
            "parental_warnings",
        ):
            values = cls._list(winner.get(field)) + cls._list(donor.get(field))
            if values:
                winner[field] = list(dict.fromkeys(values))

        winner_extra = dict(winner.get("extra") or {})
        donor_extra = donor.get("extra") if isinstance(donor.get("extra"), dict) else {}
        extra_fields = ["open_library_work_id", "dewey_decimal", "dewey_decimal_class"]
        if edition:
            extra_fields += [
                "goodreads_id",
                "lccn",
                "oclc",
                "oclc_numbers",
                "open_library_edition_id",
            ]
        for key in extra_fields:
            if not winner_extra.get(key) and donor_extra.get(key):
                winner_extra[key] = donor_extra[key]
        if winner_extra:
            winner["extra"] = winner_extra

        if edition:
            people = cls._list(winner.get("authors"))
            for person in cls._list(donor.get("authors")):
                matching = next(
                    (
                        index
                        for index, current_person in enumerate(people)
                        if cls._author_words_match(
                            cls._author_words(current_person), cls._author_words(person)
                        )
                    ),
                    None,
                )
                if matching is None:
                    people.append(cls._canonical_author_name(person))
                elif len(cls._author_words(person)) > len(
                    cls._author_words(people[matching])
                ):
                    people[matching] = cls._canonical_author_name(person)
            if people:
                winner["authors"] = people
            if not winner.get("subtitle") and ":" not in str(winner.get("title") or ""):
                parts = re.split(
                    r"\s*:\s*", cls._search_title(donor.get("title")), maxsplit=1
                )
                if len(parts) == 2:
                    winner["subtitle"] = parts[1].strip()
            for field in (
                "authors",
                "publisher",
                "published_year",
                "language",
                "isbn",
                "asin",
                "cover_url",
            ):
                value = donor.get(field)
                if field == "isbn" and value and not cls._canonical_isbn(value):
                    continue
                current = winner.get(field)
                if field == "isbn" and current and not cls._canonical_isbn(current):
                    current = None
                if (
                    not current or (isinstance(current, str) and not current.strip())
                ) and value:
                    winner[field] = value
        elif (
            cls._text(winner.get("title")) == cls._text(donor.get("title"))
            and tuple(a.casefold() for a in cls._list(winner.get("authors")))
            == tuple(a.casefold() for a in cls._list(donor.get("authors")))
            and not cls._text(winner.get("cover_url"))
            and cls._text(donor.get("cover_url"))
        ):
            # Preserve the existing cross-provider cover behavior for exact work
            # titles and author ordering when identifiers do not conflict.
            winner["cover_url"] = donor["cover_url"]
        return True

    @classmethod
    def _merge_anthology_subjects(cls, winner, donor):
        """Copy subjects only when an ISBN and most contributors corroborate a donor."""
        isbn = cls._canonical_isbn(winner.get("isbn"))
        if (
            not isbn
            or isbn != cls._canonical_isbn(donor.get("isbn"))
            or not cls._compatible_identifiers(winner, donor)
            or not cls._identity_consistent(
                dict(winner, authors=[]), dict(donor, authors=[])
            )
        ):
            return
        if not cls._anthology_contributors_match(
            winner.get("authors"), donor.get("authors")
        ):
            return
        for field in ("genres", "tags"):
            values = cls._list(winner.get(field)) + cls._list(donor.get(field))
            if values:
                winner[field] = list(dict.fromkeys(values))

    @classmethod
    def _anthology_contributors_match(cls, first, second):
        """Require most contributor names to agree across both catalog lists."""
        left = {cls._author_words(name) for name in cls._list(first)}
        right = {cls._author_words(name) for name in cls._list(second)}
        # Count overlap in both directions so an abbreviated name cannot
        # stand in for several different contributors.
        shared = min(
            sum(any(cls._author_words_match(a, b) for b in other) for a in names)
            for names, other in ((left, right), (right, left))
        )
        return shared >= 3 and shared * 5 >= 4 * max(len(left), len(right))

    @classmethod
    def _dedupe(cls, rows):
        """Retain ranked candidates and enrich only compatible matching records."""
        best = []
        for original in sorted(
            rows, key=lambda row: float(row.get("confidence") or 0), reverse=True
        ):
            row = dict(original)
            existing = next(
                (
                    item
                    for item in best
                    if cls._same_edition(item, row)
                    or (
                        cls._compatible_identifiers(item, row)
                        and cls._identity_consistent(item, row)
                        and cls._work_description_matches(item, row)
                    )
                ),
                None,
            )
            if existing is None:
                best.append(row)
            else:
                cls._merge_metadata(existing, row)

        for winner in best:
            for donor in sorted(
                rows, key=lambda row: float(row.get("confidence") or 0), reverse=True
            ):
                cls._merge_metadata(winner, donor)
        return best
