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

    def _pop(self):
        if not self._responses:
            raise AssertionError("No fake responses left")
        return self._responses.pop(0)

    async def put(self, *args, **kwargs):
        return self._pop()

    async def post(self, *args, **kwargs):
        return self._pop()


def test_bootstrap_matrix_session_success(monkeypatch):
    monkeypatch.setattr(backend, "ORG_MATRIX_SERVER_NAME", "matrix.arkavo.org")
    monkeypatch.setattr(backend, "ORG_MATRIX_HOMESERVER_URL", "http://synapse:8008")
    monkeypatch.setattr(backend, "ORG_MATRIX_ADMIN_TOKEN", "secret-admin-token")
    monkeypatch.setattr(backend, "ORG_MATRIX_PASSWORD_SECRET", "secret-password")
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(
            [
                _FakeResponse(200, payload={"name": "@org-user:matrix.arkavo.org"}),
                _FakeResponse(
                    200,
                    payload={
                        "access_token": "mx_access",
                        "user_id": "@org-user:matrix.arkavo.org",
                        "device_id": "DEVICE1",
                    },
                ),
            ]
        ),
    )

    result = asyncio.run(
        backend._bootstrap_matrix_session_for_current_user(
            {"is_anonymous": False, "pidp_id": "89c36769-8bb3-4c6b-a1d9-5b26ae41b0d7", "name": "Julian"}
        )
    )
    assert result.access_token == "mx_access"
    assert result.user_id == "@org-user:matrix.arkavo.org"


def test_bootstrap_matrix_session_requires_secret(monkeypatch):
    monkeypatch.setattr(backend, "ORG_MATRIX_PASSWORD_SECRET", "")
    try:
        asyncio.run(
            backend._bootstrap_matrix_session_for_current_user(
                {"is_anonymous": False, "pidp_id": "user-1", "name": "User One"}
            )
        )
    except backend.HTTPException as exc:
        assert exc.status_code == 503
        assert "secret" in str(exc.detail).lower()
    else:
        raise AssertionError("Expected HTTPException for missing matrix password secret")
