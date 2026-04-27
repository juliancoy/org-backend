import importlib.util
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_extract_event_candidate_from_jsonld_reads_event_fields():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Event",
      "name": "Neighborhood Assembly",
      "startDate": "2026-06-01T19:00:00Z",
      "description": "Monthly planning and voting.",
      "location": {"@type": "Place", "name": "Civic Hall"},
      "url": "https://codecollective.us/events/neighborhood-assembly"
    }
    </script>
    </head><body></body></html>
    """
    candidate = backend._extract_event_candidate_from_jsonld("https://codecollective.us/events", html)
    assert candidate["title"] == "Neighborhood Assembly"
    assert candidate["description"] == "Monthly planning and voting."
    assert candidate["location"] == "Civic Hall"
    assert candidate["source_url"] == "https://codecollective.us/events/neighborhood-assembly"
    assert candidate["starts_at"] is not None


def test_collect_event_links_from_html_filters_eventish_links():
    html = """
    <a href="/events/community-budget">Community Budget Event</a>
    <a href="/about">About</a>
    <a href="https://tickets.example.org/register/abc">Get Tickets</a>
    """
    links = backend._collect_event_links_from_html("https://codecollective.us", html, max_links=10)
    assert "https://codecollective.us/events/community-budget" in links
    assert "https://tickets.example.org/register/abc" in links
    assert all("/about" not in item for item in links)
