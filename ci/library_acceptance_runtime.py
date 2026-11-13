"""Sample and grade the acceptance library inside official Calibre."""

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from contextlib import closing
from html import unescape
from pathlib import Path
from queue import Queue
from threading import Event, Lock

from calibre.constants import numeric_version
from calibre.customize.ui import metadata_plugins
from calibre.ebooks.metadata.meta import get_file_type_metadata
from calibre.ebooks.metadata.sources.identify import merge_identify_results
from calibre.utils.logging import ThreadSafeLog
from qt.core import QImage


def save(path, data):
    """Write a checkpoint once all of its fields are available."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def load(path):
    """Read a local request or checkpoint."""
    return json.loads(path.read_text(encoding="utf-8"))


def book_fields(book):
    """Preserve the search inputs and grading fields exposed by Calibre."""
    return {
        "title": book.title,
        "authors": book.authors,
        "identifiers": book.get_identifiers(),
        "description": book.comments,
        "tags": book.tags,
        "series": book.series,
        "series_index": book.series_index if book.series else None,
    }


def query(book):
    """Return the fields Calibre passes to identify."""
    return {name: book[name] for name in ("title", "authors", "identifiers")}


def key(value):
    """Normalize case, punctuation and Unicode for independent grading."""
    return " ".join(
        re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value or "").casefold())
    )


def editions(book):
    """Normalize print and Kindle edition IDs without inferring a format."""
    result = {}
    for name, value in book["identifiers"].items():
        value = re.sub(r"[\s-]", "", str(value)).upper()
        if name == "isbn" and re.fullmatch(r"\d{9}[\dX]|\d{13}", value):
            if len(value) == 10:
                stem = "978" + value[:9]
                value = stem + str(
                    -sum(int(n) * (1 + 2 * (i % 2)) for i, n in enumerate(stem)) % 10
                )
            result.setdefault("isbn", set()).add(value)
        if name in {"asin", "amazon", "mobi-asin"} and re.fullmatch(
            r"[A-Z0-9]{10}", value
        ):
            result.setdefault("asin", set()).add(value)
    return result


def private_path(root, name):
    """Resolve a library path and reject traversal or symlinks out of the share."""
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Library file is outside the configured book root")
    return path


def inventory(db, seed, limit):
    """Select stable, stratified samples by saved evidence and lookup ID type."""
    db.row_factory = sqlite3.Row
    ids = {}
    for row in db.execute("SELECT book, type, val FROM identifiers"):
        ids.setdefault(row["book"], {})[row["type"]] = row["val"]
    groups = {}
    for row in db.execute("SELECT id, title, path, series_index FROM books"):
        book_ids = ids.get(row["id"], {})
        marked = any(
            name.startswith("metadata_engine_") and value
            for name, value in book_ids.items()
        )
        group = "processed" if marked else "no_saved_marker"
        identifier = editions({"identifiers": book_ids})
        bucket = (
            "isbn"
            if identifier.get("isbn")
            else "asin"
            if identifier.get("asin")
            else "text"
        )
        groups.setdefault((group, bucket), []).append(row)
    selected = []
    counts = {}
    for group in ("processed", "no_saved_marker"):
        buckets = [groups.get((group, kind), []) for kind in ("isbn", "asin", "text")]
        for bucket in buckets:
            bucket.sort(
                key=lambda row: hashlib.sha256(f"{seed}:{row['id']}".encode()).digest(),
                reverse=True,
            )
        counts[group] = min(limit, sum(map(len, buckets)))
        while sum(row_group == group for row_group, _ in selected) < counts[group]:
            for bucket in buckets:
                if bucket:
                    selected.append((group, bucket.pop()))
                if (
                    sum(row_group == group for row_group, _ in selected)
                    == counts[group]
                ):
                    break
    return selected, ids, counts


def linked(db, book_id, table, field):
    """Read Calibre's ordered link tables using parameters for the book ID."""
    sql = {
        "authors": "SELECT name FROM authors JOIN books_authors_link l ON authors.id=l.author WHERE l.book=? ORDER BY l.id",
        "series": "SELECT name FROM series JOIN books_series_link l ON series.id=l.series WHERE l.book=? ORDER BY l.id",
        "tags": "SELECT name FROM tags JOIN books_tags_link l ON tags.id=l.tag WHERE l.book=? ORDER BY l.id",
        "comments": "SELECT text FROM comments WHERE book=? ORDER BY id",
    }
    return [row[field] for row in db.execute(sql[table], (book_id,))]


def cover_bytes(book):
    """Return the embedded cover bytes when the format reader exposes them."""
    return book.cover_data[1] if book.cover_data and book.cover_data[1] else None


def save_case(root, cases, group, slot, origin, inputs, expected, reference_cover):
    """Freeze a standalone lookup case and its private grading evidence."""
    case_id = f"{group}-{slot:03d}-{origin}"
    directory = root / "cases" / case_id
    directory.mkdir(parents=True, exist_ok=True)
    if reference_cover:
        (directory / "reference.cover").write_bytes(reference_cover)
    save(
        directory / "case.json",
        {
            "query": query(inputs),
            "expected": expected,
            "group": group,
            "origin": origin,
        },
    )
    cases.append({"id": case_id, "group": group, "origin": origin})


def prepare(request, root):
    """Read the Calibre snapshot and book formats without writing either source."""
    books = Path(request["books"]).resolve(strict=True)
    database = Path(request["database"]).resolve(strict=True)
    cases, files = [], []
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        sample, identifiers, counts = inventory(db, request["seed"], request["sample"])
        if not sample:
            raise ValueError("No Calibre books found")
        for slot, (group, row) in enumerate(sample, 1):
            book_id = row["id"]
            series = next(iter(linked(db, book_id, "series", "name")), None)
            catalog = {
                "title": row["title"],
                "authors": linked(db, book_id, "authors", "name"),
                "identifiers": identifiers.get(book_id, {}),
                "series": series,
                "series_index": row["series_index"] if series else None,
                "tags": linked(db, book_id, "tags", "name"),
                "description": next(
                    iter(linked(db, book_id, "comments", "text")), None
                ),
            }
            folder = private_path(books, row["path"])
            path = private_path(books, folder / "cover.jpg")
            save_case(
                root,
                cases,
                group,
                slot,
                "catalog",
                catalog,
                catalog,
                path.read_bytes() if path.is_file() else None,
            )
            for fmt, name in db.execute(
                "SELECT format, name FROM data WHERE book=? ORDER BY format", (book_id,)
            ):
                fmt = fmt.lower()
                info = {"book": slot, "format": fmt}
                files.append(info)
                try:
                    path = private_path(books, folder / f"{name}.{fmt}")
                    with path.open("rb") as stream:
                        embedded = get_file_type_metadata(stream, fmt)
                    raw = book_fields(embedded)
                    info["metadata"] = raw
                    cover = cover_bytes(embedded)
                    if cover:
                        directory = root / "embedded"
                        directory.mkdir(exist_ok=True)
                        (directory / f"{slot:03d}-{fmt}.cover").write_bytes(cover)
                except (OSError, ValueError, RuntimeError):
                    info["status"] = "unreadable"
                    continue
                if not raw["title"] or key(raw["title"]) == "unknown":
                    info["status"] = "no_embedded_title"
                    continue
                if query(raw) == query(catalog):
                    info["status"] = "same_lookup_inputs"
                    continue
                info["status"] = "sampled"
                expected = dict(catalog)
                expected["identifiers"] = raw["identifiers"]
                save_case(root, cases, group, slot, fmt, raw, expected, cover)
    save(root / "embedded.local.json", files)
    save(
        root / "manifest.json",
        {"schema": 1, "seed": request["seed"], "counts": counts, "cases": cases},
    )


def title_keys(title):
    """Recognize a reading-order title and a subtitle-free variant."""
    title = re.sub(r"^(.*),\s*(The|A|An)$", r"\2 \1", title or "", flags=re.I)
    return {key(title), key(title.split(":", 1)[0])} - {""}


def author_keys(authors):
    """Handle inverted author names independently of candidate matching code."""
    return {tuple(sorted(key(name).split())) for name in authors or ()} - {
        (),
        ("unknown",),
    }


def identify_problem(candidate, expected, inputs, decision):
    """Check identity against the frozen catalog and explicit edition IDs."""
    found = editions(candidate)
    for kind, values in editions(inputs).items():
        if not values & found.get(kind, set()):
            return "edition_mismatch" if found.get(kind) else "edition_unverified"
    if not author_keys(expected["authors"]) or author_keys(
        candidate["authors"]
    ) != author_keys(expected["authors"]):
        return "author_mismatch"
    accepted = title_keys(expected["title"])
    for title in decision.get("titles", []):
        accepted |= title_keys(title)
    if not title_keys(candidate["title"]) & accepted:
        return "title_needs_review" if editions(inputs) else "title_mismatch"
    return None


def synopsis(text):
    """Strip HTML formatting before applying a minimal synopsis quality check."""
    return key(unescape(re.sub(r"<[^>]*>", " ", text or "")))


def cover_quality(path, plugin):
    """Decode and classify actual cover artwork using the existing signature."""
    if not path.is_file():
        return None
    image = QImage()
    if not image.loadFromData(path.read_bytes()):
        return None
    signature = plugin._cover_signature(image)
    return (
        image.width(),
        image.height(),
        plugin._mostly_white_cover(signature),
        signature,
    )


def grade(book, path, case, directory, reference, plugin, decision):
    """Grade completeness, regressions, and cover changes separately."""
    expected = case["expected"]
    failures, review = [], []
    identity = identify_problem(book, expected, case["query"], decision)
    if identity == "title_needs_review":
        review.append(identity)
    elif identity:
        return [identity], review
    description = synopsis(book["description"])
    if len(description.split()) < 12:
        failures.append("synopsis_missing_or_thin")
    if not any(key(tag) for tag in book["tags"]):
        failures.append("tags_missing")
    if expected["series"] and (
        key(book["series"]) != key(expected["series"])
        or book["series_index"] != expected["series_index"]
    ):
        failures.append("series_regressed_or_incorrect")
    current = cover_quality(path, plugin)
    if not current:
        failures.append("cover_missing_or_invalid")
    elif min(current[:2]) < 100:
        failures.append("cover_too_small")
    saved = cover_quality(directory / "reference.cover", plugin)
    previous = cover_quality(reference / "cover.image", plugin) if reference else None
    covers = [image for image in (saved, previous) if image]
    approved_cover = current and hashlib.sha256(
        path.read_bytes()
    ).hexdigest() in decision.get("cover_sha256", [])
    if current and covers:
        if current[2] and any(not image[2] for image in covers):
            failures.append("illustrated_cover_lost")
        matching = [
            image for image in covers if plugin._same_cover_art(current[3], image[3])
        ]
        if not matching and not current[2] and not approved_cover:
            review.append("alternate_cover_needs_review")
        if matching and current[0] * current[1] < 0.95 * max(
            image[0] * image[1] for image in matching
        ):
            failures.append("cover_resolution_regressed")
    elif current and current[2] and not approved_cover:
        review.append("white_cover_needs_review")
    if case["group"] == "processed":
        comparison = expected
        if reference:
            prior = load(reference / "result.json")
            if prior.get("metadata") and prior["status"] in {"pass", "review"}:
                comparison = prior["metadata"]
        if len(description) < 0.75 * len(
            synopsis(comparison["description"])
        ) and hashlib.sha256(description.encode()).hexdigest() not in decision.get(
            "synopsis_sha256", []
        ):
            review.append("synopsis_changed_materially")
        missing = {key(tag) for tag in comparison["tags"]} - {
            key(tag) for tag in book["tags"]
        }
        if missing - {key(tag) for tag in decision.get("dropped_tags", [])}:
            review.append("tags_changed_materially")
    return failures, review


class Evidence:
    """Capture or replay HTTP bodies at the existing plugin HTTP boundary."""

    def __init__(self, plugin, destination, source):
        """Create a private per-attempt response store."""
        self.original = plugin._http_response
        self.destination = destination
        self.source = source
        self.lock = Lock()
        self.observed = {}
        self.errors = []
        self.misses = 0

    def response(self, url, timeout):
        """Return the original response or the captured response for this request."""
        name = hashlib.sha256(url.encode()).hexdigest() + ".json"
        if self.source:
            path = self.source / name
            if not path.is_file():
                with self.lock:
                    self.misses += 1
                raise RuntimeError("No captured response for request")
            item = load(path)
            if "error" in item:
                raise RuntimeError(item["error"])
            return base64.b64decode(item["body"]), item["url"]
        try:
            body, final_url = self.original(url, timeout)
        except Exception as exc:
            with self.lock:
                self.errors.append(str(exc))
                if name not in self.observed:
                    save(self.destination / name, {"error": str(exc)})
            raise
        with self.lock:
            if name not in self.observed and len(body) <= 20 * 1024 * 1024:
                self.observed[name] = True
                save(
                    self.destination / name,
                    {
                        "url": final_url,
                        "body": base64.b64encode(body).decode("ascii"),
                    },
                )
        return body, final_url


def lookup(request, output):
    """Exercise Calibre's installed source and grade its identify and cover result."""
    case_dir = Path(request["case"])
    reference = Path(request["reference"]) if request["reference"] else None
    replay = Path(request["replay"]) / "responses" if request["replay"] else None
    case = load(case_dir / "case.json")
    plugin = next(
        source
        for source in metadata_plugins(("identify", "cover"))
        if source.name == "Metadata Engine"
    )
    plugin.prefs["base_url"] = request["engine"]
    plugin.prefs["api_token"] = os.environ.get("CALIBRE_ACCEPTANCE_TOKEN", "")
    plugin.prefs["plugin_ids"] = ""
    responses = output / "responses"
    responses.mkdir()
    evidence = Evidence(plugin, responses, replay)
    plugin._http_response = evidence.response
    log = logging.getLogger("acceptance")
    result_queue = Queue()
    abort = Event()
    timeout = request["timeout"]
    error = plugin.identify(log, result_queue, abort, **case["query"], timeout=timeout)
    candidates = list(result_queue.queue)
    for rank, candidate in enumerate(candidates):
        candidate.identify_plugin = plugin
        candidate.relevance_in_source = rank
    merged = (
        merge_identify_results({plugin: candidates}, ThreadSafeLog())
        if candidates
        else []
    )
    merged = list(merged)
    save(output / "candidates.local.json", [book_fields(book) for book in merged])
    result = {
        "plugin_version": list(plugin.version),
        "calibre_version": list(numeric_version),
        "mode": "replay" if replay else "live",
    }
    if error or not merged:
        result["failures"] = ["identify_error" if error else "no_match"]
    else:
        book = book_fields(merged[0])
        cover = output / "cover.image"
        decision = request["review"]
        identity = identify_problem(book, case["expected"], case["query"], decision)
        if identity in {None, "title_needs_review"}:
            covers = Queue()
            plugin.download_cover(
                log, covers, abort, **query(book), timeout=timeout, get_best_cover=True
            )
            if not covers.empty():
                cover.write_bytes(covers.get_nowait()[1])
        failures, review = grade(
            book, cover, case, case_dir, reference, plugin, decision
        )
        quality = cover_quality(cover, plugin)
        result.update(
            metadata=book,
            failures=failures,
            review=review,
            cover=list(quality[:3]) if quality else None,
            cover_sha256=hashlib.sha256(cover.read_bytes()).hexdigest()
            if quality
            else None,
            synopsis_sha256=hashlib.sha256(
                synopsis(book["description"]).encode()
            ).hexdigest(),
        )
    transient = [
        error
        for error in evidence.errors
        if re.search(
            r"timed out|timeout|try again|HTTP (408|429|500|502|503|504)\b", error, re.I
        )
    ]
    result["provider_transient_errors"] = len(transient)
    if evidence.misses:
        result["failures"].append("replay_capture_incomplete")
        result["status"] = "unverified"
    elif result["failures"]:
        missing = {
            "no_match",
            "identify_error",
            "cover_missing_or_invalid",
            "synopsis_missing_or_thin",
            "tags_missing",
        }
        result["status"] = (
            "transient" if transient and set(result["failures"]) <= missing else "fail"
        )
    elif result.get("review"):
        result["status"] = "review"
        result["failures"] = result["review"]
    else:
        result["status"] = "pass"
    save(output / "result.json", result)


def main():
    """Run an explicitly requested Calibre operation."""
    action, filename = sys.argv[1:]
    path = Path(filename)
    logging.basicConfig(level=logging.INFO)
    if action == "prepare":
        prepare(load(path), path.parent)
    elif action == "lookup":
        lookup(load(path), path.parent)
    else:
        raise ValueError(f"Unknown operation {action}")


if __name__ == "__main__":
    main()
