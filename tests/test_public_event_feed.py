import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def _build_event(*, seeded_from_events: bool, source_url: str | None):
    now = datetime.now(timezone.utc)
    return backend.NetworkEvent(
        id=uuid.uuid4(),
        title="Test Event",
        slug=f"test-event-{uuid.uuid4().hex[:8]}",
        starts_at=now,
        ends_at=None,
        location="Baltimore, MD",
        source_url=source_url,
        image_url=None,
        tags=["test"],
        host_type=backend.EventHostType.UNCLAIMED.value,
        host_user_id=None,
        host_org_id=None,
        claimed_by_user_id=None,
        created_by_user_id="user-1",
        seeded_from_events=seeded_from_events,
        created_at=now,
        updated_at=now,
    )


def test_map_network_event_marks_seeded_events_as_represented():
    event = _build_event(seeded_from_events=True, source_url=None)
    mapped = backend._map_network_event(event, None, object())
    assert mapped.represented_in_codecollective_source is True


def test_map_network_event_marks_codecollective_source_url_as_represented():
    event = _build_event(
        seeded_from_events=False,
        source_url="https://codecollective.us/events/community-forum",
    )
    mapped = backend._map_network_event(event, None, object())
    assert mapped.represented_in_codecollective_source is True


def test_map_network_event_marks_non_codecollective_unseeded_event_as_not_represented():
    event = _build_event(
        seeded_from_events=False,
        source_url="https://example.org/events/community-forum",
    )
    mapped = backend._map_network_event(event, None, object())
    assert mapped.represented_in_codecollective_source is False
