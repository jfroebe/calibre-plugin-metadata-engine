# -*- coding: utf-8 -*-
"""Integrate Metadata Engine searches and covers with Calibre's Source API."""

from __future__ import annotations

import json
import re
import ssl
import unicodedata
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Option, Source


class MetadataEngineSource(Source):
    """Provide Calibre metadata and cover lookup through an external engine server.

    Use configured providers or discover enabled providers. Optional bearer
    credentials and TLS preferences apply to HTTP acquisition; successful identify
    results are placed on Calibre's queue rather than written to a library here.
    """

    name = "Metadata Engine"
    description = "Download book metadata and covers from metadata-engine"
    author = "Jason Froebe"
    version = (1, 1, 23)
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
        ]
    )
    has_html_comments = False
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
            "Comma-separated provider IDs. Blank means discover all enabled providers.",
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
        query = self._make_query(title, authors, identifiers)
        if not query:
            return (
                "Metadata Engine requires a title, author, ISBN, ASIN, or identifier."
            )

        try:
            provider_ids = self._configured_provider_ids() or self._discover(
                timeout, abort
            )
            rows = []
            retried_providers = set()
            for provider_id in provider_ids:
                media_types = (None, "books") if provider_id == "itunes" else (None,)
                for media_type in media_types:
                    if abort.is_set():
                        return None
                    try:
                        if media_type:
                            found = self._search(
                                provider_id,
                                query,
                                authors,
                                identifiers,
                                timeout,
                                metadata_type=media_type,
                            )
                        else:
                            found = self._search(
                                provider_id, query, authors, identifiers, timeout
                            )
                        if provider_id == "goodreads" and not found and title:
                            short_title = self._search_title(title)
                            if short_title != str(title).strip():
                                log.info("Retrying Goodreads with a shortened title")
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
                                log.info(
                                    "Metadata Engine: contributor search provider=%s candidates=%d matches=%d",
                                    provider_id,
                                    len(retry),
                                    len(matching),
                                )
                                if matching:
                                    found.extend(matching)
                                    break
                        rows.extend(found)
                    except Exception as exc:
                        log.error(
                            "Metadata Engine provider %r failed: %s", provider_id, exc
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
                            log.info(
                                "Metadata Engine: corroborated missing contributors using provider=%s",
                                witness.get("source"),
                            )
                            break

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
            )
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
            rows = self._dedupe(rows)
            rows.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
            maximum = max(1, int(self.prefs.get("max_results", 20) or 20))

            for relevance, row in enumerate(rows[:maximum]):
                if abort.is_set():
                    return None
                mi = self._to_metadata(row, relevance)
                if mi is not None:
                    self.clean_downloaded_metadata(mi)
                    result_queue.put(mi)
            return None
        except Exception as exc:
            log.exception("Metadata Engine identify failed")
            return "Metadata Engine identify failed: %s" % exc

    def _matches_requested(self, row, title, authors, identifiers):
        """Reject ID matches that contradict the requested title or contributors."""
        if not self._identity_consistent({"title": title, "authors": authors}, row):
            return False
        matched_identifier = False
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
            title
            and authors
            and self._work_description_matches(
                {"title": title, "authors": authors}, row
            )
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
            if not self._matches_requested(row, title, authors, identifiers):
                continue
            for kind in ("isbn", "asin", "goodreads", "lccn", "oclc", "openlibrary"):
                value = value_for(row, kind)
                if not value or not re.fullmatch(patterns[kind], value):
                    continue
                if kind == "isbn" and not self._canonical_isbn(value):
                    log.warning(
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
            log.warning(
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
                    continue
                pair = (provider, kind, value)
                if pair in visited:
                    continue
                if len(visited) >= 12:
                    log.warning(
                        "Metadata Engine: identifier follow-up stopped at 12 requests"
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
                            log.warning(
                                "Metadata Engine: rejected conflicting identity evidence provider=%s kind=%s",
                                provider,
                                kind,
                            )
                            continue
                        accepted.append(candidate)
                    rows.extend(accepted)
                    log.info(
                        "Metadata Engine: identifier follow-up provider=%s kind=%s accepted=%d",
                        provider,
                        kind,
                        len(accepted),
                    )
                except Exception as exc:
                    log.error(
                        "Metadata Engine identifier follow-up provider %s kind %s failed: %s",
                        provider,
                        kind,
                        exc,
                    )
        return rows

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

        matches = [row for row in self._dedupe(rows) if requested_match(row)]
        missing = [row for row in matches if not self._text(row.get("description"))]
        if not missing:
            return rows
        candidate = missing[0]
        short_title = self._search_title(candidate.get("title"))
        if not short_title or not self._list(candidate.get("authors")):
            log.warning(
                "Metadata Engine: matching result has no description; insufficient title or author data for recovery"
            )
            return rows

        log.info(
            "Metadata Engine: matching result has no description; trying configured sources"
        )
        for provider_id in provider_ids:
            if abort.is_set():
                return rows
            # These providers only support identifiers, already used above.
            if provider_id in {"asintojson", "isbnlab"} or provider_id in (
                retried_providers or set()
            ):
                continue
            # Avoid issuing the same title-only request twice.
            if not authors and not identifiers and query.strip() == short_title:
                continue
            try:
                kwargs = {"metadata_type": "books"} if provider_id == "itunes" else {}
                found = self._search(
                    provider_id, short_title, None, {}, timeout, **kwargs
                )
                log.info(
                    "Metadata Engine: description retry provider=%r candidates=%d",
                    provider_id,
                    len(found),
                )
                for winner in rows:
                    if not requested_match(winner):
                        continue
                    for donor in found:
                        before = self._text(winner.get("description"))
                        if self._merge_metadata(winner, donor):
                            if not before and self._text(winner.get("description")):
                                log.info(
                                    "Metadata Engine: description supplied by provider=%r",
                                    provider_id,
                                )
                if not any(
                    requested_match(row) and not self._text(row.get("description"))
                    for row in self._dedupe(rows)
                ):
                    log.info("Metadata Engine: matching description recovered")
                    return rows
            except Exception as exc:
                log.error(
                    "Metadata Engine description retry provider %r failed: %s",
                    provider_id,
                    exc,
                )
        log.warning(
            "Metadata Engine: matching result remains without a description after configured sources"
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
        """Queue matching covers ranked by edition evidence and image dimensions."""
        identifiers = identifiers or {}
        candidates = []

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
            try:
                provider_ids = self._configured_provider_ids() or self._discover(
                    timeout, abort
                )
            except Exception as exc:
                log.error("Metadata Engine provider discovery failed: %s", exc)
                provider_ids = []

            rows = []
            for provider_id in provider_ids:
                if abort.is_set():
                    return None
                try:
                    provider_rows = self._search(
                        provider_id, query, authors, identifiers, timeout
                    )
                    if provider_id == "itunes":
                        provider_rows.extend(
                            self._search(
                                provider_id,
                                query,
                                authors,
                                identifiers,
                                timeout,
                                metadata_type="books",
                            )
                        )
                    if (
                        title
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
                            self._work_description_matches(
                                {"title": title, "authors": authors}, item
                            )
                            for item in provider_rows
                        )
                    ):
                        for person in [None, *self._list(authors)[:3]]:
                            if abort.is_set():
                                return None
                            retry = self._search(
                                provider_id,
                                self._search_title(title),
                                [person] if person else None,
                                {},
                                timeout,
                                **(
                                    {"metadata_type": "books"}
                                    if provider_id == "itunes"
                                    else {}
                                ),
                            )
                            matching = [
                                item
                                for item in retry
                                if self._work_description_matches(
                                    {"title": title, "authors": authors}, item
                                )
                            ]
                            log.info(
                                "Metadata Engine cover retry provider=%s candidates=%d matches=%d",
                                provider_id,
                                len(retry),
                                len(matching),
                            )
                            if matching:
                                provider_rows.extend(matching)
                                break
                    rows.extend(provider_rows)
                    cover_count = sum(
                        1 for row in provider_rows if row.get("cover_url")
                    )
                    if cover_count:
                        log.info(
                            "Metadata Engine provider %r returned %d cover candidate(s)",
                            provider_id,
                            cover_count,
                        )
                except Exception as exc:
                    # A cover lookup is an aggregate provider operation. One provider
                    # failing (for example NYT returning 502) must not suppress covers
                    # returned by Open Library, Google Books, Hardcover, etc.
                    log.error(
                        "Metadata Engine cover provider %r failed: %s",
                        provider_id,
                        exc,
                    )

            rows.extend(
                self._recover_cover_records(
                    log, abort, provider_ids, rows, title, authors, identifiers, timeout
                )
            )

            target = {"title": title, "authors": authors}
            eligible = []
            missing_cover = rejected_identity = 0
            for row in rows:
                if not row.get("cover_url"):
                    missing_cover += 1
                    continue
                score = self._cover_identifier_score(row, identifiers)
                if (
                    not score
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
            log.info(
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
            candidates.extend(rows)

        from urllib.parse import urlsplit

        from qt.core import QImage

        seen_urls = set()
        covers = []
        # Bound network work while still allowing several sources and sizes.
        for row in candidates[:12]:
            if abort.is_set():
                return None
            url = self._text(row.get("cover_url"))
            if not url:
                continue
            urls = [url]
            host = (urlsplit(url).hostname or "").casefold()
            if host == "mzstatic.com" or host.endswith(".mzstatic.com"):
                larger = re.sub(r"/\d+x\d+bb(?=\.)", "/2200x2200bb", url)
                if larger != url:
                    urls.insert(0, larger)
            for candidate_url in urls:
                if candidate_url in seen_urls:
                    continue
                seen_urls.add(candidate_url)
                try:
                    data = self._http_bytes(candidate_url, min(timeout, 10))
                    if not self._looks_like_image(data):
                        raise ValueError("downloaded content is not a recognized image")
                    image = QImage.fromData(data)
                    if image.isNull() or image.width() <= 0 or image.height() <= 0:
                        raise ValueError("downloaded image cannot be decoded")
                    score = self._cover_identifier_score(row, identifiers)
                    covers.append(
                        (
                            score,
                            image.width() * image.height(),
                            row,
                            candidate_url,
                            data,
                        )
                    )
                    log.info(
                        "Metadata Engine cover candidate source=%s size=%dx%d identifier_score=%d",
                        self._text(row.get("source")) or "unknown",
                        image.width(),
                        image.height(),
                        score,
                    )
                    # Keep the original as fallback if an enlarged URL fails.
                    break
                except Exception as exc:
                    log.error(
                        "Metadata Engine cover candidate failed source=%r url=%s error=%s",
                        row.get("source"),
                        candidate_url,
                        exc,
                    )

        if covers:
            covers.sort(key=lambda item: (item[0], item[1]), reverse=True)
            for score, pixels, row, url, data in covers:
                if abort.is_set():
                    return None
                result_queue.put((self, data))
                if get_best_cover:
                    break
            self._remember_cover(covers[0][2])
            return None

        if candidates:
            log.error(
                "Metadata Engine exhausted %d cover candidate(s)", len(candidates)
            )
        else:
            log.error("Metadata Engine returned no cover candidates")
        return None

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
        target = {"title": title, "authors": authors}
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
                if abort.is_set():
                    return recovered
                pair = (provider, value)
                if pair in visited:
                    continue
                visited.add(pair)
                if not re.fullmatch(pattern, value):
                    log.warning(
                        "Metadata Engine cover: invalid provider ID provider=%s",
                        provider,
                    )
                    continue
                try:
                    found = self._search(provider, prefix + value, None, {}, timeout)
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
                            log.warning(
                                "Metadata Engine cover: rejected conflicting provider record provider=%s id=%s",
                                provider,
                                value,
                            )
                            continue
                        if row.get("cover_url"):
                            recovered.append(row)
                            accepted += 1
                    log.info(
                        "Metadata Engine cover: provider=%s id=%s records=%d with_cover=%d",
                        provider,
                        value,
                        len(found),
                        accepted,
                    )
                except Exception as exc:
                    log.error(
                        "Metadata Engine cover: provider=%s id=%s lookup failed: %s",
                        provider,
                        value,
                        exc,
                    )
        return recovered

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
        language = self._identifier(identifiers, "language", "lang")
        if isbn:
            params["isbn"] = isbn
        if asin:
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

    def _to_metadata(self, row, relevance):
        if not self._book_media(row):
            return None
        title = self._text(row.get("title"))
        if not title:
            return None
        # Some books carry their series only in a trailing "Series Book N".
        # Keep a structured provider series authoritative when available.
        inferred_series = None
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:Book\s*|#)(\d+(?:\.\d+)?)\)", title, re.IGNORECASE
        )
        if match:
            series_name = match.group(2).strip()
            # A numbered franchise prefix (e.g. "Aeon 14:") is not the series.
            series_name = re.sub(r"^.+?\s+\d+\s*:\s*", "", series_name)
            if series_name:
                title = match.group(1).strip()
                inferred_series = (series_name, float(match.group(3)))
        subtitle = self._text(row.get("subtitle"))
        if subtitle and subtitle.casefold() not in title.casefold():
            title = "%s: %s" % (title, subtitle)

        authors = self._list(row.get("authors")) or ["Unknown"]
        mi = Metadata(title, authors)
        mi.source_relevance = relevance

        if row.get("publisher"):
            mi.publisher = self._text(row["publisher"])
        if row.get("description"):
            description = self._text(row["description"]) or ""
            # Remove explicit source-attribution lines, not names in prose.
            description = re.sub(
                r"(?im)^\s*(?:metadata )?source\s*:\s*(?:amazon(?:\.com)?|audible|goodreads|hardcover|itunes|apple books|open library|google books|isbnlab|asintojson)\s*$",
                "",
                description,
            ).strip()
            if description:
                mi.comments = description
        if row.get("language"):
            mi.languages = [self._text(row["language"])]

        # Provider names, shop navigation and format labels are not subjects.
        source = (self._text(row.get("source")) or "").casefold()
        excluded = {
            "amazon",
            "amazon.com",
            "audible",
            "audiobook",
            "audiobooks",
            "goodreads",
            "hardcover",
            "itunes",
            "apple books",
            "open library",
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
        for tag in tags:
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

        if not mi.series and inferred_series:
            mi.series, mi.series_index = inferred_series

        if row.get("cover_url"):
            mi.has_cached_cover = True
            self._remember_cover(row)

        extras = []
        narrators = self._list(row.get("narrators"))
        if narrators:
            extras.append("Narrator(s): " + ", ".join(narrators))
        if row.get("duration_seconds"):
            try:
                seconds = int(row["duration_seconds"])
                hours, rem = divmod(seconds, 3600)
                minutes = rem // 60
                extras.append("Duration: %dh %02dm" % (hours, minutes))
            except Exception:
                pass
        for field, label in (
            ("trigger_warnings", "Trigger warnings"),
            ("parental_warnings", "Parental warnings"),
        ):
            warnings = self._list(row.get(field))
            existing = (mi.comments or "").casefold()
            missing = [value for value in warnings if value.casefold() not in existing]
            if missing:
                extras.append(label + ": " + "; ".join(dict.fromkeys(missing)))
        if extras:
            mi.comments = (mi.comments or "") + "\n\n" + "\n".join(extras)
        return mi

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
            url = self._cover_urls.get((str(key).lower(), str(value)))
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
        headers = {
            "Accept": "application/json, image/*;q=0.9, */*;q=0.1",
            "User-Agent": "Calibre-Metadata-Engine/1.1.23",
            "Accept-Encoding": "identity",
        }
        token = str(self.prefs.get("api_token", "") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(url, headers=headers)
        context = None
        if url.lower().startswith("https://") and not self.prefs.get(
            "verify_ssl", True
        ):
            context = ssl._create_unverified_context()
        try:
            with urlopen(
                req, timeout=max(1, int(timeout)), context=context
            ) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("HTTP %s: %s" % (exc.code, detail or exc.reason))
        except URLError as exc:
            raise RuntimeError(str(exc.reason))

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
        wanted_isbn = cls._identifier(identifiers, "isbn", "isbn13", "isbn10")
        row_isbn = cls._text(row.get("isbn"))
        if wanted_isbn and row_isbn:

            def normalize(value):
                return "".join(
                    ch for ch in str(value).upper() if ch.isdigit() or ch == "X"
                )

            if normalize(wanted_isbn) == normalize(row_isbn):
                return 3
            return 0

        wanted_asin = cls._identifier(identifiers, "asin")
        row_asin = cls._text(row.get("asin"))
        if wanted_asin and row_asin and wanted_asin.upper() == row_asin.upper():
            return 2
        if wanted_asin and row_asin:
            return 0

        wanted_goodreads = cls._identifier(
            identifiers, "goodreads", "metadata_engine_goodreads"
        )
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        row_goodreads = cls._text(extra.get("goodreads_id"))
        if not row_goodreads and row.get("source") == "goodreads":
            row_goodreads = cls._text(row.get("source_id"))
        if wanted_goodreads and row_goodreads:
            return 2 if wanted_goodreads == row_goodreads else 0

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
            return str(title).strip()
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

    @classmethod
    def _search_title(cls, value):
        """Remove a numbered series suffix without changing provider query case."""
        title = cls._text(value) or ""
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:book\s*|#)(\d+(?:\.\d+)?)\)",
            title,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else title

    @classmethod
    def _work_title(cls, value):
        """Extract a numbered series suffix only for matching book works."""
        title = unicodedata.normalize("NFKC", cls._text(value) or "").casefold()
        title = title.translate(str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"}))
        title = " ".join(title.split())
        match = re.fullmatch(
            r"(.+?)\s+\((.+?)\s+(?:book\s*|#)(\d+(?:\.\d+)?)\)",
            title,
            re.IGNORECASE,
        )
        if not match:
            return title, None, None
        series = re.sub(r"^.+?\s+\d+\s*:\s*", "", match.group(2)).strip()
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
    def _work_titles_compatible(cls, left, right):
        """Match an inserted series name only when another record names that series."""
        left_title, _, _ = cls._work_title(left.get("title"))
        right_title, _, _ = cls._work_title(right.get("title"))
        if not left_title or not right_title:
            return False
        if left_title == right_title:
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

            def normalize(name):
                if "," in name:
                    parts = [part.strip() for part in name.split(",", 1)]
                    if all(parts):
                        name = parts[1] + " " + parts[0]
                return re.sub(r"[\W_]+", "", name.casefold())

            return {value for value in (normalize(name) for name in people) if value}

        left_people, right_people = names(winner), names(donor)
        # One source may list a subset of an anthology's authors or identify
        # an editor as its author. Require a nonempty subset, never mere overlap
        # between two conflicting contributor lists.
        return bool(
            left_people
            and right_people
            and (left_people <= right_people or right_people <= left_people)
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

        for field in ("description", "series"):
            if not winner.get(field) or (
                isinstance(winner[field], str) and not winner[field].strip()
            ):
                value = donor.get(field)
                if value and (not isinstance(value, str) or value.strip()):
                    winner[field] = value
        # Merge work-level subjects and warning labels from compatible records.
        for field in ("genres", "tags", "trigger_warnings", "parental_warnings"):
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
