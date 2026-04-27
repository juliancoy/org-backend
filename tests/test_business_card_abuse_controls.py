import asyncio
import importlib.util
from pathlib import Path
import sys

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def _make_request(headers: list[tuple[bytes, bytes]] | None = None, client_host: str | None = None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers or [],
    }
    if client_host is not None:
        scope["client"] = (client_host, 12345)
    return backend.Request(scope)


def test_request_client_ip_uses_forwarded_for_first_address():
    request = _make_request(
        headers=[(b"x-forwarded-for", b"203.0.113.10, 10.0.0.1")],
        client_host="198.51.100.9",
    )
    assert backend._request_client_ip(request) == "203.0.113.10"


def test_request_client_ip_falls_back_to_client_host():
    request = _make_request(client_host="198.51.100.9")
    assert backend._request_client_ip(request) == "198.51.100.9"


def test_request_client_ip_invalid_returns_unknown():
    request = _make_request(headers=[(b"x-forwarded-for", b"not-an-ip")])
    assert backend._request_client_ip(request) == "unknown"


class _FakeQuery:
    def __init__(self, count_value: int):
        self._count_value = count_value

    def filter(self, *args, **kwargs):
        return self

    def scalar(self):
        return self._count_value


class _FakeSession:
    def __init__(self, count_value: int):
        self._count_value = count_value

    def query(self, *args, **kwargs):
        return _FakeQuery(self._count_value)


def test_duplicate_hash_guard_blocks_when_limit_reached():
    session = _FakeSession(count_value=3)
    try:
        backend._enforce_business_card_duplicate_hash_guard(
            session,
            image_sha256="abc",
            duplicate_hash_limit=3,
            duplicate_hash_window_seconds=3600,
        )
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 429


def test_get_business_card_settings_forbidden_for_non_sysadmin():
    try:
        asyncio.run(
            backend.get_business_card_abuse_settings(
                current_user={"is_anonymous": False, "is_sysadmin": False}
            )
        )
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403


class _FakeConn:
    def __init__(self):
        self.execute_calls = []

    async def execute(self, *args):
        self.execute_calls.append(args)


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


def test_update_business_card_settings_as_sysadmin(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(backend.db, "async_pool", _FakePool(fake_conn))

    async def _noop_ensure():
        return None

    async def _fake_get():
        return {
            "enabled": True,
            "per_user_limit_per_hour": 5,
            "per_ip_limit_per_hour": 10,
            "global_limit_per_hour": 20,
            "duplicate_hash_limit": 2,
            "duplicate_hash_window_seconds": 1800,
            "max_bytes": 1024 * 1024,
            "allowed_content_types": ["image/jpeg"],
            "event_link_enrichment_enabled": True,
            "updated_at": backend.datetime.now(backend.timezone.utc),
            "updated_by": "sysadmin@example.com",
        }

    monkeypatch.setattr(backend, "ensure_business_card_runtime_settings_table", _noop_ensure)
    monkeypatch.setattr(backend, "get_business_card_runtime_settings", _fake_get)

    payload = backend.BusinessCardAbuseSettingsUpdate(
        per_user_limit_per_hour=5,
        allowed_content_types=["image/jpeg", "image/png"],
        event_link_enrichment_enabled=False,
    )
    result = asyncio.run(
        backend.update_business_card_abuse_settings(
            payload=payload,
            current_user={"is_anonymous": False, "is_sysadmin": True, "email": "sysadmin@example.com"},
        )
    )

    assert result["per_user_limit_per_hour"] == 5
    assert fake_conn.execute_calls, "Expected DB execute call"
    _, *params = fake_conn.execute_calls[0]
    assert "image/jpeg,image/png" in params
    assert False in params


def test_extract_public_urls_from_text_normalizes_and_dedupes():
    text = """
    Event details:
    www.codecollective.us/events/townhall
    https://codecollective.us/events/townhall
    https://example.org/register
    """
    urls = backend._extract_public_urls_from_text(text, max_urls=10)
    assert "https://codecollective.us/events/townhall" in urls
    assert "https://example.org/register" in urls
    assert len(urls) == 2
