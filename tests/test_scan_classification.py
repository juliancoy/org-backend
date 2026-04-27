import importlib.util
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_score_scan_kind_prefers_person_when_email_present():
    scores = backend._score_scan_kind_candidates(
        ocr_text="Jane Doe\njane@example.com\nSenior Organizer",
        extracted_person={
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+1-555-0100",
            "title": "Senior Organizer",
            "company": "Code Collective",
        },
        extracted_org={},
        extracted_event={},
    )
    assert scores["person"] > scores["organization"]
    assert scores["person"] > scores["event"]


def test_detect_scan_kind_prefers_event_when_datetime_and_location_present():
    scores = backend._score_scan_kind_candidates(
        ocr_text="Community Meeting Event\nApr 30 7PM\nCivic Hall",
        extracted_person={},
        extracted_org={"name": "Community Alliance"},
        extracted_event={
            "title": "Community Meeting",
            "starts_at": object(),
            "location": "Civic Hall",
            "website": "https://example.org/events/community-meeting",
        },
    )
    assert scores["event"] >= 0.8
    detected = backend._detect_scan_kind(
        ocr_text="Community Meeting Event\nApr 30 7PM\nCivic Hall",
        extracted_person={},
        extracted_org={"name": "Community Alliance"},
        extracted_event={
            "title": "Community Meeting",
            "starts_at": object(),
            "location": "Civic Hall",
            "website": "https://example.org/events/community-meeting",
        },
    )
    assert detected == "event"
