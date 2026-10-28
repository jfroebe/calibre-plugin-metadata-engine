# -*- coding: utf-8 -*-
"""Integrate Metadata Engine searches and covers with Calibre's Source API."""

from __future__ import annotations

import json
import re
import ssl
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
    version = (1, 1, 11)
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
        ]
    )
    has_html_comments = False
    supports_gzip_transfer_encoding = True
    cached_cover_url_is_reliable = False
    can_get_multiple_covers = False
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
        Option(
            "include_source_tag",
            "bool",
            False,
            "Add source tag",
            "Add metadata-engine:<source> to Calibre tags.",
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
                            short_title, _, _ = self._work_title(title)
                            if short_title != str(title).strip().casefold():
                                log.info("Retrying Goodreads with a shortened title")
                                found = self._search(
                                    provider_id, short_title, None, {}, timeout
                                )
                                retried_providers.add(provider_id)
                        rows.extend(found)
                    except Exception as exc:
                        log.error(
                            "Metadata Engine provider %r failed: %s", provider_id, exc
                        )

            # An exact edition identifier ties the provider result to the
            # existing Calibre record even when the provider omits its authors.
            requested_asin = self._identifier(identifiers, "asin")
            if requested_asin and authors:
                for row in rows:
                    row_asin = self._text(row.get("asin"))
                    if (
                        not self._list(row.get("authors"))
                        and row_asin
                        and row_asin.upper() == requested_asin.upper()
                    ):
                        row["authors"] = self._list(authors)

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
    ):
        """Retry configured title-search providers for an incomplete matching work."""

        def requested_match(row):
            matched_identifier = False
            for key, aliases in (
                ("asin", ("asin",)),
                ("isbn", ("isbn", "isbn13", "isbn10")),
            ):
                wanted = self._identifier(identifiers, *aliases)
                actual = self._text(row.get(key))
                if wanted and actual:
                    normalized_wanted = re.sub(r"[\s-]+", "", wanted).upper()
                    normalized_actual = re.sub(r"[\s-]+", "", actual).upper()
                    if normalized_wanted != normalized_actual:
                        return False
                    matched_identifier = True
            return matched_identifier or bool(
                title
                and authors
                and self._work_description_matches(
                    {"title": title, "authors": authors}, row
                )
            )

        matches = [row for row in self._dedupe(rows) if requested_match(row)]
        missing = [row for row in matches if not self._text(row.get("description"))]
        if not missing:
            return rows
        candidate = missing[0]
        short_title, _, _ = self._work_title(candidate.get("title"))
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
            if provider_id in {"asintojson", "isbnlab"} or provider_id in (retried_providers or set()):
                continue
            # Avoid issuing the same title-only request twice.
            if (
                not authors
                and not identifiers
                and query.strip().casefold() == short_title
            ):
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
                rows.extend(found)
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
        """Queue the first valid cover from cached or newly searched candidate URLs.

        Rank searched candidates by identifier match and confidence, skip failed or
        non-image downloads, and honor abort between operations. Queue (self, bytes)
        on success and return None; get_best_cover is accepted for Calibre API
        compatibility but does not alter this implementation's selection.
        """
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

            rows = [
                r
                for r in rows
                if r.get("cover_url") and self._cover_identifier_score(r, identifiers)
            ]
            rows.sort(
                key=lambda x: (
                    self._cover_identifier_score(x, identifiers),
                    float(x.get("confidence") or 0),
                ),
                reverse=True,
            )
            candidates.extend(rows)

        seen_urls = set()
        for row in candidates:
            if abort.is_set():
                return None
            url = self._text(row.get("cover_url"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                data = self._http_bytes(url, timeout)
                if not self._looks_like_image(data):
                    raise ValueError("downloaded content is not a recognized image")
                result_queue.put((self, data))
                self._remember_cover(row)
                log.info(
                    "Metadata Engine cover selected from %s: %s",
                    self._text(row.get("source")) or "unknown",
                    url,
                )
                return None
            except Exception as exc:
                log.error(
                    "Metadata Engine cover candidate failed source=%r url=%s error=%s",
                    row.get("source"),
                    url,
                    exc,
                )

        if candidates:
            log.error(
                "Metadata Engine exhausted %d cover candidate(s)", len(candidates)
            )
        else:
            log.error("Metadata Engine returned no cover candidates")
        return None

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
            r"(.+?)\s+\((.+?)\s+Book\s+(\d+(?:\.\d+)?)\)", title, re.IGNORECASE
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
            mi.comments = self._text(row["description"])
        if row.get("language"):
            mi.languages = [self._text(row["language"])]

        provider_tags = self._list(row.get("tags"))
        source_name = str(row.get("source") or "").casefold()
        if source_name in {"hardcover", "itunes"}:
            # These providers inject their source name into their own tag list.
            provider_tags = [
                tag for tag in provider_tags if tag.casefold() != source_name
            ]
        tags = self._list(row.get("genres")) + provider_tags
        if self.prefs.get("include_source_tag", False) and row.get("source"):
            tags.append("metadata-engine:%s" % self._text(row["source"]))
        mi.tags = list(dict.fromkeys(tags))

        if row.get("isbn"):
            mi.set_identifier("isbn", self._text(row["isbn"]))
        if row.get("asin"):
            mi.set_identifier("asin", self._text(row["asin"]))

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
        if extras:
            mi.comments = (mi.comments or "") + "\n\n" + "\n".join(extras)
        return mi

    def _remember_cover(self, row):
        url = self._text(row.get("cover_url"))
        if not url:
            return
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
            "User-Agent": "Calibre-Metadata-Engine/1.1.11",
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
    def _work_title(cls, value):
        """Extract a numbered series suffix only for matching book works."""
        title = (cls._text(value) or "").casefold()
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
    def _work_description_matches(cls, winner, donor):
        """Require matching work title, complete author set and compatible series."""
        left, left_series, left_number = cls._work_title(winner.get("title"))
        right, right_series, right_number = cls._work_title(donor.get("title"))
        if not left or left != right:
            return False
        if (
            left_series
            and right_series
            and (left_series != right_series or left_number != right_number)
        ):
            return False

        def authors(row):
            return {
                re.sub(r"[\W_]+", "", name.casefold())
                for name in cls._list(row.get("authors"))
                if re.sub(r"[\W_]+", "", name.casefold())
            }

        left_authors, right_authors = authors(winner), authors(donor)
        return bool(left_authors and left_authors == right_authors)

    @classmethod
    def _dedupe(cls, rows):
        """Keep ranked identity fields and fill missing content from duplicates.

        Only rows with the same existing edition/source key contribute content.
        Never overwrite a populated description or cover with an empty value.
        Cover downloading separately retains every URL for transport fallback.
        """
        best = {}
        for original in sorted(
            rows, key=lambda row: float(row.get("confidence") or 0), reverse=True
        ):
            row = dict(original)
            if row.get("isbn"):
                key = ("isbn", str(row["isbn"]).replace("-", "").replace(" ", ""))
            elif row.get("asin"):
                key = ("asin", str(row["asin"]).upper())
            elif row.get("source") and row.get("source_id"):
                key = (
                    "source",
                    str(row["source"]).casefold(),
                    str(row["source_id"]).casefold(),
                )
            else:
                key = (
                    "book",
                    str(row.get("title") or "").casefold(),
                    tuple(a.casefold() for a in cls._list(row.get("authors"))),
                )
            old = best.get(key)
            if old is None:
                best[key] = row
            else:
                for field in ("description", "cover_url"):
                    if not cls._text(old.get(field)) and cls._text(row.get(field)):
                        old[field] = row[field]
        for winner in best.values():
            authors = tuple(a.casefold() for a in cls._list(winner.get("authors")))
            if not authors:
                continue
            for donor in sorted(
                rows, key=lambda row: float(row.get("confidence") or 0), reverse=True
            ):
                if cls._work_description_matches(winner, donor):
                    conflict = any(
                        winner.get(key)
                        and donor.get(key)
                        and str(winner[key]).replace("-", "").replace(" ", "").upper()
                        != str(donor[key]).replace("-", "").replace(" ", "").upper()
                        for key in ("isbn", "asin")
                    )
                    if (
                        not conflict
                        and not cls._text(winner.get("description"))
                        and cls._text(donor.get("description"))
                    ):
                        winner["description"] = cls._text(donor.get("description"))
                if cls._text(winner.get("title")) != cls._text(donor.get("title")):
                    continue
                if authors != tuple(
                    a.casefold() for a in cls._list(donor.get("authors"))
                ):
                    continue
                if any(
                    winner.get(key)
                    and donor.get(key)
                    and str(winner[key]).replace("-", "").replace(" ", "").upper()
                    != str(donor[key]).replace("-", "").replace(" ", "").upper()
                    for key in ("isbn", "asin")
                ):
                    continue
                for field in ("description", "cover_url"):
                    if not cls._text(winner.get(field)) and cls._text(donor.get(field)):
                        winner[field] = donor[field]
                winner["genres"] = list(
                    dict.fromkeys(
                        cls._list(winner.get("genres")) + cls._list(donor.get("genres"))
                    )
                )
        return list(best.values())
