import asyncio
import importlib.util
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        if not self._responses:
            raise AssertionError("No fake responses left")
        return self._responses.pop(0)


def test_extract_business_card_fields_from_text():
    text = """
    Jane Doe
    Senior Engineer
    Example Labs
    jane.doe@example.com
    +1 (410) 555-1212
    www.examplelabs.org
    123 Main Street, Baltimore, MD 21201
    """
    parsed = backend._extract_business_card_fields(text)
    assert parsed["name"] == "Jane Doe"
    assert parsed["title"] == "Senior Engineer"
    assert parsed["company"] == "Example Labs"
    assert parsed["email"] == "jane.doe@example.com"
    assert "410" in (parsed["phone"] or "")
    assert "examplelabs.org" in (parsed["website"] or "")


def test_create_or_find_pidp_user_created(monkeypatch):
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [_FakeResponse(201, payload={"id": "pidp-123"})]
        ),
    )
    result = asyncio.run(
        backend._create_or_find_pidp_user_from_business_card(
            email="new.user@example.com",
            full_name="New User",
        )
    )
    assert result["created"] is True
    assert result["pidp_user_id"] == "pidp-123"
    assert result["generated_password"]


def test_create_or_find_pidp_user_existing(monkeypatch):
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [_FakeResponse(409, payload={"detail": "already exists"})]
        ),
    )
    result = asyncio.run(
        backend._create_or_find_pidp_user_from_business_card(
            email="existing.user@example.com",
            full_name="Existing User",
        )
    )
    assert result["created"] is False
    assert result["pidp_user_id"] is None


def test_extract_event_fields_from_text():
    text = """
    Community Budget Town Hall
    May 10, 2026 7:30 PM
    Location: City Hall, Baltimore
    https://codecollective.us/events/townhall
    """
    parsed = backend._extract_event_fields_from_text(text)
    assert parsed["title"] == "Community Budget Town Hall"
    assert parsed["starts_at"] is not None
    assert "city hall" in (parsed["location"] or "").lower()
    assert "codecollective.us" in (parsed["website"] or "")


def test_extract_events_from_text_handles_multi_event_flyer():
    text = """
    RED LINE
    OPEN HOUSES
    May 2, 2026:
    10:00 am-12:00 pm
    Woodlawn High School

    May 5, 2026:
    3:00-5:00 pm
    Baltimore War Memorial Building

    May 7, 2026:
    6:00-8:00 pm
    Edmondson-Westside High School

    May 9, 2026:
    11:00 am-1:00 pm
    Enoch Pratt Southeast Anchor Library
    """
    parsed = backend._extract_events_from_text(text)
    assert len(parsed) == 4
    assert all(item.get("starts_at") is not None for item in parsed)
    locations = [str(item.get("location") or "").lower() for item in parsed]
    assert any("woodlawn high school" in value for value in locations)
    assert any("war memorial" in value for value in locations)
    assert any("edmondson-westside high school" in value for value in locations)
    assert any("southeast anchor library" in value for value in locations)


def test_extract_organization_fields_from_text():
    text = """
    Code Collective Foundation
    Civic infrastructure and community governance.
    www.codecollective.us
    """
    parsed = backend._extract_organization_fields_from_text(text)
    assert parsed["name"] == "Code Collective Foundation"
    assert "codecollective.us" in (parsed["website"] or "")
    assert "civic infrastructure" in (parsed["description"] or "").lower()


def test_detect_scan_kind_prefers_person_with_email():
    text = "Jane Doe\njane@example.com\nwww.example.com"
    person = backend._extract_business_card_fields(text)
    org = backend._extract_organization_fields_from_text(text)
    event = backend._extract_event_fields_from_text(text)
    assert backend._detect_scan_kind(text, person, org, event) == "person"


def test_detect_scan_kind_finds_event_without_email():
    text = "Neighborhood Cleanup Event\nJun 1, 2026 9:00 AM\nMain Street"
    person = backend._extract_business_card_fields(text)
    org = backend._extract_organization_fields_from_text(text)
    event = backend._extract_event_fields_from_text(text)
    assert backend._detect_scan_kind(text, person, org, event) == "event"


def test_derive_org_payload_for_person_scan_prefers_company():
    payload = backend._derive_org_payload_for_person_scan(
        extracted_person={
            "name": "Andy Olek",
            "company": "Olek Law Offices LLC",
            "website": "oleklaw.com",
            "raw_lines": ["Andy Olek", "Olek Law Offices LLC", "andy@oleklaw.com"],
        },
        extracted_org={
            "name": "Andy Olek",
            "website": "https://oleklaw.com",
            "description": "Legal advice and services",
            "raw_lines": ["Olek Law Offices LLC"],
        },
    )
    assert payload is not None
    assert payload["name"] == "Olek Law Offices LLC"
    assert "oleklaw.com" in (payload["website"] or "")


def test_derive_org_payload_for_person_scan_uses_website_when_company_missing():
    payload = backend._derive_org_payload_for_person_scan(
        extracted_person={
            "name": "Alex Smith",
            "company": None,
            "website": "www.examplelegal.com",
            "raw_lines": [],
        },
        extracted_org={
            "name": "Alex Smith",
            "website": None,
            "description": None,
            "raw_lines": [],
        },
    )
    assert payload is not None
    assert payload["name"] == "Examplelegal"
    assert "examplelegal.com" in (payload["website"] or "")


def test_scan_created_target_image_url_for_public_entities():
    submission_id = "d867dc26-7bb2-4f2d-898d-5c4de6aac86a"
    url = backend._scan_created_target_image_url(
        backend.uuid.UUID(submission_id),
        target_type="organization",
        target_id="5be17652-f7c9-4dca-b8b8-d7b4d2f307f5",
    )
    assert url == (
        "/api/network/scans/d867dc26-7bb2-4f2d-898d-5c4de6aac86a/"
        "image/public/organization/5be17652-f7c9-4dca-b8b8-d7b4d2f307f5"
    )
    person_url = backend._scan_created_target_image_url(
        backend.uuid.UUID(submission_id),
        target_type="person",
        target_id="89c36769-8bb3-4c6b-a1d9-5b26ae41b0d7",
    )
    assert person_url is None


def test_summarize_scan_targets_with_openai_single_pass(monkeypatch):
    monkeypatch.setattr(backend, "ORG_SCAN_AI_SUMMARY_ENABLED", True)
    monkeypatch.setattr(backend, "ORG_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [
                _FakeResponse(
                    200,
                    payload={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "items": [
                                                {"index": 0, "summary": "Legal advice and services company record."},
                                                {"index": 1, "summary": "Founder contact profile extracted from card."},
                                            ]
                                        }
                                    )
                                }
                            }
                        ]
                    },
                )
            ]
        ),
    )
    targets = [
        {"type": "organization", "id": "org-1", "name": "Olek Law"},
        {"type": "person", "id": "person-1", "name": "Andy Olek"},
    ]
    summarized = asyncio.run(
        backend._summarize_scan_targets_with_openai(
            ocr_text="Olek Law\nAndy Olek\nLegal advice and services",
            created_targets=targets,
        )
    )
    assert len(summarized) == 2
    assert summarized[0]["summary"] == "Legal advice and services company record."
    assert summarized[1]["summary"] == "Founder contact profile extracted from card."
