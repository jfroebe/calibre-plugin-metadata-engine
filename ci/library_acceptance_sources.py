"""Check saved failures against alternate catalogs on the Calibre worker."""

import base64
import binascii
import json
import os
import re
from collections import Counter
from pathlib import Path
from threading import Event
from urllib.parse import urlencode, urlsplit

from calibre.customize.ui import metadata_plugins
from library_acceptance_runtime import (
    author_keys,
    book_fields,
    cover_quality,
    editions,
    grade,
    key,
    load,
    save,
    synopsis,
    title_keys,
)

PROVIDERS = (
    "goodreads",
    "google_books",
    "open_library",
    "hardcover",
    "isbnlab",
    "loc_json_yaml",
    "loc_sru",
    "itunes",
    "asintojson",
    "audible",
    "audnexus",
)
TEMPORARY = re.compile(r"timed out|timeout|HTTP (408|429|500|502|503|504)\b", re.I)


def plugin_for(engine):
    """Configure the installed plugin for independent provider requests."""
    plugin = next(
        p for p in metadata_plugins(("identify",)) if p.name == "Metadata Engine"
    )
    plugin.prefs["base_url"] = engine
    plugin.prefs["api_token"] = os.getenv("CALIBRE_ACCEPTANCE_TOKEN", "")
    plugin.prefs["plugin_ids"] = ""
    return plugin


def identity(book, case):
    """Require title and authors, and distinguish exact edition from work evidence."""
    expected = case["expected"]
    wanted, found = editions(case["query"]), editions(book)
    if any(
        kind in found and values.isdisjoint(found[kind])
        for kind, values in wanted.items()
    ):
        return "different_edition"
    if not (title_keys(expected["title"]) & title_keys(book["title"])):
        return "different_title"
    if not author_keys(expected["authors"]) or author_keys(
        expected["authors"]
    ) != author_keys(book["authors"]):
        return "different_authors"
    if wanted and all(
        not values.isdisjoint(found.get(kind, ())) for kind, values in wanted.items()
    ):
        return "exact_edition"
    return "work_match"


def get_attempt(run, case_id, build):
    """Return the completed attempt used by the pilot report."""
    matches = sorted((run / case_id / build).glob("attempt-*/result.json"))
    return matches[-1].parent if matches else None


def captured(directory):
    """Decode successful cached provider payloads without making network calls."""
    if directory is None:
        return []
    out = []
    for path in (directory / "responses").glob("*.json"):
        value = load(path)
        try:
            parsed = json.loads(base64.b64decode(value.get("body", "")))
        except (ValueError, UnicodeError, binascii.Error):
            continue
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            continue
        url = urlsplit(value.get("url", ""))
        parts = url.path.split("/")
        if len(parts) >= 4 and parts[-3] == "plugins" and parts[-1] == "search":
            out.extend(
                (parts[-2], row) for row in parsed["results"] if isinstance(row, dict)
            )
    return out


def public_rows(plugin, case, timeout, errors):
    """Look up exact Open Library ISBN data and Google Books independently."""
    params = {"q": "intitle:" + case["expected"]["title"], "maxResults": 5}
    wanted = editions(case["query"])
    isbn = next(iter(sorted(wanted.get("isbn", ()))), None)
    if isbn:
        params["q"] = "isbn:" + isbn
        bib = "ISBN:" + isbn
        query = urlencode({"bibkeys": bib, "format": "json", "jscmd": "data"})
        try:
            record = plugin._http_json(
                "https://openlibrary.org/api/books?" + query, timeout
            ).get(bib)
            if record:
                ids = record.get("identifiers", {})
                reported = [*ids.get("isbn_13", []), *ids.get("isbn_10", [])]
                yield (
                    "open_library",
                    {
                        "title": record.get("title"),
                        "authors": [a["name"] for a in record.get("authors", [])],
                        "isbn": next(
                            (
                                value
                                for value in reported
                                if isbn
                                in editions({"identifiers": {"isbn": value}}).get(
                                    "isbn", ()
                                )
                            ),
                            None,
                        ),
                        "genres": [item["name"] for item in record.get("subjects", [])],
                        "cover_url": record.get("cover", {}).get("large"),
                    },
                )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            errors["open_library_direct"] = str(exc)
    try:
        data = plugin._http_json(
            "https://www.googleapis.com/books/v1/volumes?" + urlencode(params), timeout
        )
        for item in data.get("items", []):
            info = item.get("volumeInfo", {})
            links = info.get("imageLinks", {})
            ids = {
                entry.get("type"): entry.get("identifier")
                for entry in info.get("industryIdentifiers", [])
            }
            yield (
                "google_books",
                {
                    "title": info.get("title"),
                    "authors": info.get("authors", []),
                    "isbn": ids.get("ISBN_13") or ids.get("ISBN_10"),
                    "description": info.get("description"),
                    "genres": info.get("categories", []),
                    "cover_url": links.get("extraLarge")
                    or links.get("large")
                    or links.get("thumbnail"),
                },
            )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        errors["google_books_direct"] = str(exc)


def enabled_sources(plugin, timeout):
    """Discover configured sources while retaining direct catalog fallbacks."""
    try:
        return set(plugin._discover(timeout, Event()))
    except (OSError, RuntimeError, ValueError):
        return set()


def live_rows(plugin, case, timeout, enabled, errors):
    """Try useful alternate providers serially to limit provider load."""
    target = case["query"]
    identifiers = dict(target["identifiers"])
    isbn = next(iter(sorted(editions(target).get("isbn", ()))), None)
    if isbn:
        identifiers["isbn"] = isbn
    for provider in PROVIDERS:
        if provider not in enabled:
            continue
        if provider in {"audible", "audnexus"} and not plugin._audiobook_cover_lookup(
            identifiers
        ):
            continue
        try:
            for row in plugin._search(
                provider,
                case["expected"]["title"],
                case["expected"]["authors"],
                identifiers,
                timeout,
            ):
                yield provider, row
        except (OSError, RuntimeError, ValueError) as exc:
            errors[provider] = str(exc)
    yield from public_rows(plugin, case, timeout, errors)


def corroborate(plugin, case, rows):
    """Keep the attributed evidence and reject unrelated title or author records."""
    found = []
    rejected = Counter()
    for source, row in rows:
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            continue
        try:
            mapped = plugin._to_metadata(dict(row, source=source), 0)
        except (AttributeError, TypeError, ValueError):
            rejected["invalid_provider_record"] += 1
            continue
        if mapped is None:
            continue
        book = book_fields(mapped)
        verdict = identity(book, case)
        if verdict not in {"work_match", "exact_edition"}:
            rejected[verdict] += 1
            continue
        evidence = {
            "source": source,
            "identity": verdict,
            "metadata": book,
            "cover_url": row.get("cover_url"),
        }
        if evidence not in found:
            found.append(evidence)
    return found, dict(rejected)


def verify_artwork(plugin, case_dir, sources, returned, timeout, errors):
    """Decode a bounded number of edition-verified covers and compare artwork."""
    from qt.core import QImage

    reference = cover_quality(case_dir / "reference.cover", plugin)
    for entry in [
        row
        for row in sources
        if row["identity"] == "exact_edition" and row["cover_url"]
    ][:6]:
        url = entry["cover_url"]
        if not isinstance(url, str) or urlsplit(url).scheme not in {"https", "http"}:
            continue
        host = urlsplit(url).hostname or ""
        allowed = (
            "google.com",
            "googleusercontent.com",
            "googleapis.com",
            "openlibrary.org",
            "goodreads.com",
            "gr-assets.com",
            "media-amazon.com",
            "images-amazon.com",
            "amazonaws.com",
            "mzstatic.com",
            "archive.org",
            "hardcover.app",
        )
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            continue
        try:
            image = QImage()
            if not image.loadFromData(plugin._http_bytes(url, timeout)):
                continue
            signature = plugin._cover_signature(image)
            if (
                plugin._mostly_white_cover(signature)
                or min(image.width(), image.height()) < 100
            ):
                continue
            entry["cover_dimensions"] = [image.width(), image.height()]
            entry["matches_saved_art"] = bool(
                reference and plugin._same_cover_art(signature, reference[3])
            )
            size = returned.get("cover") or [0, 0]
            entry["larger_cover"] = image.width() * image.height() > size[0] * size[1]
        except (OSError, RuntimeError, ValueError) as exc:
            errors[entry["source"] + "_cover"] = str(exc)


def findings(reasons, sources, errors, returned, expected):
    """Explain failed fields using the independent catalog evidence."""
    hits = {}
    for source in sources:
        book = source["metadata"]
        fields = {
            "cover_missing_or_invalid": bool(source.get("cover_dimensions")),
            "cover_resolution_regressed": source.get("larger_cover")
            and source.get("matches_saved_art"),
            "alternate_cover_needs_review": source.get("matches_saved_art"),
            "no_match": True,
            "identify_error": True,
            "edition_unverified": source["identity"] == "exact_edition",
            "edition_mismatch": source["identity"] == "exact_edition",
            "author_mismatch": bool(book["authors"]),
            "tags_missing": bool(book["tags"]),
            "tags_changed_materially": bool(
                (
                    {key(tag) for tag in book["tags"]}
                    & {key(tag) for tag in expected["tags"]}
                )
                - {key(tag) for tag in returned.get("tags", [])}
            ),
            "synopsis_missing_or_thin": len(synopsis(book["description"]).split())
            >= 12,
            "synopsis_changed_materially": len(synopsis(book["description"]))
            >= 0.75 * len(synopsis(expected["description"])),
            "series_regressed_or_incorrect": bool(book["series"])
            and key(book["series"]) == key(expected["series"])
            and book["series_index"] == expected["series_index"],
        }
        for reason in reasons:
            if fields.get(reason):
                hits.setdefault(reason, set()).add(source["source"])
    return {
        "supported": {reason: sorted(names) for reason, names in hits.items()},
        "unresolved": sorted(set(reasons) - hits.keys()),
        "provider_transient": sorted(
            name for name, value in errors.items() if TEMPORARY.search(value)
        ),
    }


def diagnose(request, output):
    """Regrade the saved outputs and check alternate sources without rerunning the pilot."""
    plugin = plugin_for(request["engine"])
    run = Path(request["run"])
    state = run.parents[1]
    directory = run / "diagnostics"
    directory.mkdir(exist_ok=True)
    timeout = request["timeout"]
    enabled = enabled_sources(plugin, timeout)
    by_source = Counter()
    counts = Counter()
    recovered = Counter()
    unresolved = Counter()
    provider_errors = Counter()
    diagnoses = Counter()
    for row in load(run / "report.local.json")["cases"]:
        case_id = row["id"]
        case_dir = state / "cases" / case_id
        case = load(case_dir / "case.json")
        results = {}
        for build in ("baseline", "candidate"):
            result = row[build]
            attempt = get_attempt(run, case_id, build)
            if result.get("metadata") and attempt:
                failures, review = grade(
                    result["metadata"],
                    attempt / "cover.image",
                    case,
                    case_dir,
                    plugin,
                    {},
                )
                status = "fail" if failures else "review" if review else "pass"
                if failures and result["status"] == "transient":
                    status = "transient"
                results[build] = (status, failures + review)
            else:
                results[build] = (result["status"], result.get("failures", []))
        counts[results["baseline"][0] + " -> " + results["candidate"][0]] += 1
        if results["candidate"][0] == "pass":
            continue
        cache = directory / (case_id + ".json")
        if cache.exists():
            evidence = load(cache)
        else:
            errors = {}
            rows = []
            for build in ("baseline", "candidate"):
                rows.extend(captured(get_attempt(run, case_id, build)))
            sources, rejected = corroborate(plugin, case, rows)
            for source in sources:
                source["origin"] = "captured"
            returned = row["candidate"].get("metadata") or {}
            reasons = results["candidate"][1]
            previous = findings(reasons, sources, errors, returned, case["expected"])
            if previous["unresolved"]:
                fresh, more_rejected = corroborate(
                    plugin, case, live_rows(plugin, case, timeout, enabled, errors)
                )
                for item in fresh:
                    if item not in [
                        {k: v for k, v in source.items() if k != "origin"}
                        for source in sources
                    ]:
                        sources.append(dict(item, origin="alternate"))
                rejected = dict(Counter(rejected) + Counter(more_rejected))
            if any("cover" in reason for reason in reasons):
                verify_artwork(
                    plugin, case_dir, sources, row["candidate"], timeout, errors
                )
            captured_findings = findings(
                reasons,
                [item for item in sources if item["origin"] == "captured"],
                errors,
                returned,
                case["expected"],
            )
            evidence = {
                "captured_supported": captured_findings["supported"],
                "sources": sources,
                "rejected": rejected,
                "errors": errors,
                **findings(reasons, sources, errors, returned, case["expected"]),
            }
            save(cache, evidence)
        recovered.update(evidence["supported"].keys())
        by_source.update(
            {name for names in evidence["supported"].values() for name in names}
        )
        unresolved.update(evidence["unresolved"])
        provider_errors.update(evidence["provider_transient"])
        if results["candidate"][0] == "review":
            cause = "catalog_reference_difference"
        elif evidence.get("captured_supported"):
            cause = "matching_catalog_data_in_original_responses"
        elif evidence["supported"]:
            cause = "matching_data_in_alternate_catalog"
        elif evidence["provider_transient"] or row["candidate"].get(
            "provider_transient_errors"
        ):
            cause = "provider_errors_with_no_confirmed_alternative"
        else:
            cause = "insufficient_catalog_evidence"
        diagnoses[cause] += 1
        print(
            json.dumps(
                {
                    "case": case_id,
                    "cause": cause,
                    "supported": evidence["supported"],
                    "unresolved": evidence["unresolved"],
                    "provider_transient": evidence["provider_transient"],
                }
            ),
            flush=True,
        )
    summary = {
        "transitions": dict(counts),
        "corroborated": dict(recovered),
        "unresolved": dict(unresolved),
        "provider_transient": dict(provider_errors),
        "diagnoses": dict(diagnoses),
        "supporting_catalogs": dict(by_source),
    }
    save(directory / "summary.json", summary)
    print("DIAGNOSTIC_SUMMARY " + json.dumps(summary), flush=True)
