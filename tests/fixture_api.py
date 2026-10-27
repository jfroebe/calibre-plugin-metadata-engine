"""Deterministic fault server; deliberately distinct from real-engine tests."""

from __future__ import annotations

import json
import struct
import time
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

ROWS = json.loads(Path(__file__).with_name("fixtures.json").read_text("utf-8"))


def png(width: int = 120, height: int = 180) -> bytes:
    """Return a synthetic, decodable RGB cover with no external licensing."""

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    raw = (b"\0" + b"\x30\x70\xa0" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def select(query: str, isbn: str = "", asin: str = "") -> list[dict]:
    """Select deterministic editions with exact identifier precedence."""
    if isbn:
        return [dict(row) for row in ROWS if row.get("isbn") == isbn]
    if asin:
        return [dict(row) for row in ROWS if row.get("asin") == asin]
    return [dict(row) for row in ROWS if row["title"].casefold() in query.casefold()]


@contextmanager
def serve():
    """Bind only loopback, capture requests without headers, then clean up."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        """Serve fixture routes and explicitly injected transport failures."""

        def log_message(self, *args):
            """Suppress default URL and credential-bearing diagnostic logs."""

        def do_GET(self):
            """Respond to the three client API routes and cover requests."""
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            requests.append((path.path, query))
            status, payload = 200, {}
            content_type = "application/json"
            if path.path == "/cover.png":
                payload, content_type = png(), "image/png"
            elif path.path == "/full.png":
                payload, content_type = png(600, 900), "image/png"
            elif path.path == "/cached.png":
                payload, content_type = png(), "image/png"
            elif path.path == "/thumbnail.png":
                payload, content_type = png(20, 30), "image/png"
            elif path.path == "/invalid.png":
                payload = b"<html>not an image</html>"
            elif path.path == "/health":
                payload = {"ok": True}
            elif path.path == "/plugins":
                payload = {
                    "plugins": [
                        {"plugin_id": "broken", "enabled": True},
                        {"plugin_id": "fixture", "enabled": True},
                        {"plugin_id": "disabled", "enabled": False},
                    ]
                }
            elif path.path == "/plugins/specter/search":
                payload = {"results": []}
                if query.get("asin") == ["B08CK7H7P2"]:
                    payload = {
                        "results": [
                            {
                                "title": "A Specter and an Invasion",
                                "authors": [
                                    "M. D. Cooper",
                                    "Chris J. Pike",
                                    "Jill Cooper",
                                ],
                                "asin": "B08CK7H7P2",
                                "description": "Fixture description",
                                "metadata_type": "books",
                                "confidence": 0.99,
                            }
                        ]
                    }
            elif path.path == "/plugins/broken/search":
                status, payload = 502, {"detail": "controlled provider outage"}
            elif path.path == "/plugins/unauthorized/search":
                status, payload = 401, {"detail": "unauthorized"}
            elif path.path == "/plugins/malformed/search":
                payload = b"{broken json"
            elif path.path == "/bad-discovery/plugins":
                payload = {"plugins": "incompatible"}
            elif path.path.startswith("/plugins/schema-"):
                payload = {
                    "array": [],
                    "missing": {"items": []},
                    "mapping": {"results": {}},
                    "row": {"results": ["incompatible"]},
                    "title": {"results": [{"title": ["incompatible"]}]},
                }[path.path.split("/")[2].removeprefix("schema-")]
            elif path.path in {
                "/plugins/mixed/search",
                "/plugins/editions/search",
                "/plugins/wrong-edition/search",
            }:
                row = dict(
                    ROWS[0],
                    metadata_type="book",
                    confidence=0.5,
                    cover_url=self.server.base_url + "/full.png",
                    thumbnail_url=self.server.base_url + "/thumbnail.png",
                )
                wrong = dict(
                    row,
                    isbn="9780441172719",
                    confidence=1.0,
                    cover_url=self.server.base_url + "/thumbnail.png",
                )
                movie = dict(
                    row,
                    title="The Hobbit movie",
                    metadata_type="movie",
                    confidence=1.0,
                    cover_url=self.server.base_url + "/invalid.png",
                )
                rows = [row, movie] if "mixed" in path.path else [wrong, row]
                if "wrong-edition" in path.path:
                    rows = [wrong]
                payload = {"results": rows}
            elif path.path == "/plugins/slow/search":
                time.sleep(2)
                payload = {"results": []}
            elif path.path == "/plugins/curvy-merge/search":
                payload = {
                    "results": [
                        {
                            "title": "Curvy",
                            "authors": ["Alexa Riley"],
                            "confidence": 0.98,
                            "source_id": "curvy-no-isbn",
                            "description": "A matching book description.",
                            "cover_url": self.server.base_url + "/full.png",
                        },
                        {
                            "title": "Curvy",
                            "authors": ["Alexa Riley"],
                            "confidence": 0.98,
                            "source_id": "curvy-edition",
                            "isbn": "9781978363632",
                            "description": "A matching book description.",
                        },
                        {
                            "title": "My Curvy Belle",
                            "authors": ["Jordan Silver"],
                            "source_id": "other-book",
                            "isbn": "9781717900326",
                            "confidence": 0.76,
                        },
                    ]
                }
            elif path.path in {
                "/plugins/sparse/search",
                "/plugins/rich/search",
                "/plugins/bad-cover/search",
            }:
                provider = path.path.split("/")[2]
                row = dict(ROWS[0], source=provider, confidence=0.99)
                row.update(description=None, cover_url=None)
                if provider == "rich":
                    row.update(
                        confidence=0.8,
                        description="The matching edition's full description.",
                        cover_url=self.server.base_url + "/full.png",
                    )
                elif provider == "bad-cover":
                    row["cover_url"] = self.server.base_url + "/invalid.png"
                payload = {"results": [row]}
            elif path.path == "/plugins/fixture/search":
                rows = select(
                    query.get("query", [""])[0],
                    query.get("isbn", [""])[0],
                    query.get("asin", [""])[0],
                )
                for row in rows:
                    row.update(
                        source="fixture",
                        source_id=row["title"],
                        confidence=0.9,
                        cover_url=self.server.base_url + "/cover.png",
                    )
                payload = {"results": rows}
            else:
                status, payload = 404, {"detail": "missing provider"}
            body = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.base_url, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
