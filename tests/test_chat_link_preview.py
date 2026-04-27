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
    def __init__(self, *, status_code: int, url: str, text: str, headers: dict[str, str]):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers
        self.content = text.encode("utf-8")

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self._response


def test_fetch_chat_link_preview_extracts_og_metadata(monkeypatch):
    html = """
    <html>
      <head>
        <title>Ignored Title</title>
        <meta property="og:title" content="Preview Title" />
        <meta property="og:description" content="Preview Description" />
        <meta property="og:image" content="/img/preview.png" />
        <meta property="og:site_name" content="Preview Site" />
        <link rel="canonical" href="https://example.com/canonical-article" />
      </head>
      <body>hello</body>
    </html>
    """
    fake_response = _FakeResponse(
        status_code=200,
        url="https://example.com/article",
        text=html,
        headers={"content-type": "text/html; charset=utf-8"},
    )
    monkeypatch.setattr(
        backend.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(fake_response),
    )

    result = asyncio.run(backend._fetch_chat_link_preview("https://example.com/article"))
    assert result.title == "Preview Title"
    assert result.description == "Preview Description"
    assert result.canonical_url == "https://example.com/canonical-article"
    assert result.image_url == "https://example.com/img/preview.png"
    assert result.site_name == "Preview Site"
    assert result.domain == "example.com"


def test_fetch_chat_link_preview_rejects_private_hosts():
    try:
        asyncio.run(backend._fetch_chat_link_preview("http://127.0.0.1/internal"))
    except backend.HTTPException as exc:
        assert exc.status_code == 422
        assert "public hostname" in str(exc.detail).lower()
    else:
        raise AssertionError("Expected HTTPException for private link preview URL")
