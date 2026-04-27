import importlib.util
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_extract_contact_import_from_html():
    html = """
    <html>
      <head><title>Julian Coy | Code Collective</title></head>
      <body>
        <section id="main">
          <h1>Julian Coy</h1>
          Julian Coy is a Baltimore-based Python expert.
          <h2>Contact Information</h2>
          <a href="mailto:julian@codecollective.us">Email</a>
          <a href="tel:4102587550">Phone</a>
          <a href="https://www.linkedin.com/in/julian-coy-a2906415/">LinkedIn</a>
          <a href="https://github.com/julianfl0w">GitHub</a>
        </section>
      </body>
    </html>
    """
    extracted = backend._extract_contact_import_from_html(
        "https://codecollective.us/personnel/juliancoy.html",
        html,
    )
    assert extracted["headline"] == "Julian Coy"
    assert extracted["email_public"] == "julian@codecollective.us"
    assert extracted["phone_public"] == "4102587550"
    assert extracted["linkedin_url"] == "https://www.linkedin.com/in/julian-coy-a2906415/"
    assert extracted["github_url"] == "https://github.com/julianfl0w"


def test_ensure_public_fetch_url_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        backend.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(backend.HTTPException) as exc:
        backend._ensure_public_fetch_url("https://example.com/path")
    assert exc.value.status_code == 422
    assert "public host" in str(exc.value.detail).lower()


def test_apply_contact_import_to_record_updates_socials():
    contact = backend.UserContactPage(
        user_id="u1",
        slug="u1",
        enabled=False,
        links=[],
    )
    changed = backend._apply_contact_import_to_record(
        contact,
        {
            "headline": "Platform Engineer",
            "linkedin_url": "https://www.linkedin.com/in/example/",
            "github_url": "https://github.com/example",
            "x_url": "https://x.com/example",
            "links": [{"label": "Website", "url": "https://example.com"}],
        },
        overwrite=True,
    )
    assert "headline" in changed
    assert "linkedin_url" in changed
    assert "github_url" in changed
    assert "x_url" in changed
    assert contact.github_url == "https://github.com/example"
    assert contact.x_url == "https://x.com/example"
    assert isinstance(contact.links, list)
    assert contact.links[0]["url"] == "https://example.com"
