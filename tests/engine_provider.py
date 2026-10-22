"""Test-only local fixture provider; never installed in production."""

from fixture_api import select
from metadata_engine.models import MetadataCandidate
from metadata_engine.plugin_api import MetadataPlugin


class FixtureProvider(MetadataPlugin):
    """Supply deterministic book editions through the engine's real cache."""

    plugin_id = "calibre_fixture"
    display_name = "Calibre fixture"
    author = "Test suite"
    license = "GPL-3.0-only"
    version = "1.0.0"
    metadata_types = ["book"]
    access_target = "test fixtures"
    calls = 0

    async def search(self, request):
        """Count calls so tests can prove cache hits skip provider execution."""
        self.calls += 1
        rows = select(request.query, request.isbn or "", request.asin or "")
        return [
            MetadataCandidate(
                **dict(
                    row,
                    source=self.plugin_id,
                    source_id=row["title"],
                    confidence=0.95,
                    metadata_type="book",
                    cover_url=self.config["cover_base"] + "/test-cover.png",
                )
            )
            for row in rows
        ]
