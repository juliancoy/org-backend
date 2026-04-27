import asyncio
import importlib.util
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

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

    async def get(self, *args, **kwargs):
        if not self._responses:
            raise AssertionError("No fake responses left")
        return self._responses.pop(0)


def test_fetch_pidp_identity_supports_jwt(monkeypatch):
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [_FakeResponse(200, payload={"id": "u1", "email": "u1@example.com", "full_name": "User One"})]
        ),
    )
    identity = asyncio.run(backend._fetch_pidp_identity("jwt-token"))
    assert identity["pidp_id"] == "u1"
    assert identity["pidp_is_sysadmin"] is False
    assert identity["token_kind"] == "jwt"
    assert identity["token_scope"] == "session"


def test_fetch_pidp_identity_supports_pat(monkeypatch):
    monkeypatch.setattr(backend, "ORG_ALLOWED_PAT_SCOPES", {"org_portal", "org_mcp", "org_admin"})
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [
                _FakeResponse(401, payload={"detail": "Invalid credentials"}),
                _FakeResponse(
                    200,
                    payload={
                        "token_kind": "pat",
                        "scope": "org_portal",
                        "scope_grants": ["org:profile.read", "org:profile.write"],
                        "owner": {
                            "id": "u2",
                            "email": "u2@example.com",
                            "full_name": "User Two",
                            "is_sysadmin": True,
                        },
                    },
                ),
            ]
        ),
    )
    identity = asyncio.run(backend._fetch_pidp_identity("pidp_pat_token"))
    assert identity["pidp_id"] == "u2"
    assert identity["pidp_is_sysadmin"] is True
    assert identity["token_kind"] == "pat"
    assert identity["token_scope"] == "org_portal"


def test_fetch_pidp_identity_rejects_disallowed_pat_scope(monkeypatch):
    monkeypatch.setattr(backend, "ORG_ALLOWED_PAT_SCOPES", {"org_portal"})
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [
                _FakeResponse(401, payload={"detail": "Invalid credentials"}),
                _FakeResponse(
                    200,
                    payload={
                        "token_kind": "pat",
                        "scope": "service",
                        "scope_grants": ["service:*"],
                        "owner": {
                            "id": "u3",
                            "email": "u3@example.com",
                            "full_name": "User Three",
                        },
                    },
                ),
            ]
        ),
    )
    try:
        asyncio.run(backend._fetch_pidp_identity("pidp_pat_token"))
    except backend.HTTPException as exc:
        assert exc.status_code == 403
        assert "not allowed" in str(exc.detail).lower()
    else:
        raise AssertionError("Expected HTTPException for disallowed PAT scope")
