# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import ssl
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Option, Source


class MetadataEngineSource(Source):
    name = "Metadata Engine"
    description = "Download book metadata and covers from metadata-engine"
    author = "Jason Froebe"
    version = (1, 1, 2)
    minimum_calibre_version = (6, 0, 0)
    supported_platforms = ["windows", "osx", "linux"]

    capabilities = frozenset(["identify", "cover"])
    touched_fields = frozenset([
        "title", "authors", "publisher", "pubdate", "comments", "languages",
        "tags", "series", "series_index", "identifier:isbn", "identifier:asin",
    ])
    has_html_comments = False
    supports_gzip_transfer_encoding = True
    cached_cover_url_is_reliable = False
    can_get_multiple_covers = False

    options = (
        Option("base_url", "string", "http://127.0.0.1:8790", "Metadata Engine URL",
               "Base URL of metadata-engine."),
        Option("plugin_ids", "string", "", "Metadata Engine plugin IDs",
               "Comma-separated provider IDs. Blank means discover all enabled providers."),
        Option("max_results", "number", 20, "Maximum results",
               "Maximum metadata results returned to Calibre."),
        Option("per_plugin_limit", "number", 10, "Per-provider result limit",
               "Maximum results requested from each metadata-engine provider."),
        Option("api_token", "string", "", "Bearer token",
               "Optional HTTP Bearer token."),
        Option("verify_ssl", "bool", True, "Verify HTTPS certificates",
               "Verify TLS certificates for HTTPS connections."),
        Option("include_source_tag", "bool", False, "Add source tag",
               "Add metadata-engine:<source> to Calibre tags."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cover_urls = {}

    def is_configured(self):
        return bool(self._base_url())

    def config_widget(self):
        from calibre_plugins.metadata_engine.config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.commit()

    def identify(self, log, result_queue, abort, title=None, authors=None,
                 identifiers=None, timeout=30):
        identifiers = identifiers or {}
        query = self._make_query(title, authors, identifiers)
        if not query:
            return "Metadata Engine requires a title, author, ISBN, ASIN, or identifier."

        try:
            provider_ids = self._configured_provider_ids() or self._discover(timeout, abort)
            rows = []
            for provider_id in provider_ids:
                if abort.is_set():
                    return None
                try:
                    rows.extend(self._search(provider_id, query, authors, identifiers, timeout))
                except Exception as exc:
                    log.error("Metadata Engine provider %r failed: %s", provider_id, exc)

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

    def download_cover(self, log, result_queue, abort, title=None, authors=None,
                       identifiers=None, timeout=30, get_best_cover=False):
        identifiers = identifiers or {}
        candidates = []

        cached = self._cached_cover(identifiers)
        if cached:
            candidates.append({
                "cover_url": cached,
                "confidence": 1.0,
                "source": "cache",
            })

        query = self._make_query(title, authors, identifiers)
        if query:
            try:
                provider_ids = self._configured_provider_ids() or self._discover(timeout, abort)
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
                    cover_count = sum(1 for row in provider_rows if row.get("cover_url"))
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

            rows = [r for r in self._dedupe(rows) if r.get("cover_url")]
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
            log.error("Metadata Engine exhausted %d cover candidate(s)", len(candidates))
        else:
            log.error("Metadata Engine returned no cover candidates")
        return None

    def _discover(self, timeout, abort):
        if abort.is_set():
            return []
        payload = self._http_json(self._base_url() + "/plugins", timeout)
        found = []
        for item in payload.get("plugins") or []:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict) and item.get("enabled", True):
                pid = item.get("plugin_id") or item.get("id") or item.get("name")
                if pid:
                    found.append(str(pid))
        return found

    def _search(self, provider_id, query, authors, identifiers, timeout):
        params = {
            "query": query,
            "limit": max(1, min(100, int(self.prefs.get("per_plugin_limit", 10) or 10))),
        }
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
            self._base_url(), quote(str(provider_id), safe=""), urlencode(params)
        )
        data = self._http_json(url, timeout)
        out = []
        for row in data.get("results") or []:
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("source", provider_id)
                out.append(row)
        return out

    def _to_metadata(self, row, relevance):
        title = self._text(row.get("title"))
        if not title:
            return None
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

        tags = self._list(row.get("genres")) + self._list(row.get("tags"))
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
            mi.set_identifier("metadata_engine_%s" % source.lower().replace(" ", "_"), source_id)

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
                mi.series = self._text(item.get("name") or item.get("title") or item.get("series"))
                idx = item.get("index", item.get("position", item.get("number")))
                try:
                    mi.series_index = float(idx) if idx is not None else None
                except Exception:
                    pass

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
        return json.loads(self._http_bytes(url, timeout).decode("utf-8"))

    def _http_bytes(self, url, timeout):
        headers = {"Accept": "application/json, image/*;q=0.9, */*;q=0.1",
                   "User-Agent": "Calibre-Metadata-Engine/1.1.2",
                   "Accept-Encoding": "identity"}
        token = str(self.prefs.get("api_token", "") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(url, headers=headers)
        context = None
        if url.lower().startswith("https://") and not self.prefs.get("verify_ssl", True):
            context = ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=max(1, int(timeout)), context=context) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("HTTP %s: %s" % (exc.code, detail or exc.reason))
        except URLError as exc:
            raise RuntimeError(str(exc.reason))

    def _base_url(self):
        return str(self.prefs.get("base_url", "") or "").strip().rstrip("/")

    def _configured_provider_ids(self):
        return [x.strip() for x in str(self.prefs.get("plugin_ids", "") or "").split(",") if x.strip()]

    @classmethod
    def _cover_identifier_score(cls, row, identifiers):
        """Prefer an exact edition identifier before generic confidence ranking."""
        wanted_isbn = cls._identifier(identifiers, "isbn", "isbn13", "isbn10")
        row_isbn = cls._text(row.get("isbn"))
        if wanted_isbn and row_isbn:
            normalize = lambda value: "".join(
                ch for ch in str(value).upper() if ch.isdigit() or ch == "X"
            )
            if normalize(wanted_isbn) == normalize(row_isbn):
                return 3

        wanted_asin = cls._identifier(identifiers, "asin")
        row_asin = cls._text(row.get("asin"))
        if wanted_asin and row_asin and wanted_asin.upper() == row_asin.upper():
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
            if lower.get(key):
                return str(lower[key]).strip()
        return None

    @classmethod
    def _make_query(cls, title, authors, identifiers):
        parts = []
        if title:
            parts.append(str(title).strip())
        parts.extend(str(a).strip() for a in (authors or []) if str(a).strip())
        for key in ("isbn", "isbn13", "isbn10", "asin"):
            value = cls._identifier(identifiers, key)
            if value:
                parts.append(value)
        if not parts:
            parts.extend(str(v).strip() for v in identifiers.values() if v)
        return " ".join(parts)

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
    def _dedupe(cls, rows):
        best = {}
        for row in rows:
            if row.get("isbn"):
                key = ("isbn", str(row["isbn"]).replace("-", "").replace(" ", ""))
            elif row.get("asin"):
                key = ("asin", str(row["asin"]).upper())
            elif row.get("source") and row.get("source_id"):
                key = ("source", str(row["source"]).casefold(), str(row["source_id"]).casefold())
            else:
                key = ("book", str(row.get("title") or "").casefold(),
                       tuple(a.casefold() for a in cls._list(row.get("authors"))))
            old = best.get(key)
            if old is None or float(row.get("confidence") or 0) > float(old.get("confidence") or 0):
                best[key] = row
        return list(best.values())
