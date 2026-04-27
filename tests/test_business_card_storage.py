import importlib.util
from pathlib import Path
import sys
import uuid

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_business_card_extension_for_content_type():
    assert backend._business_card_extension_for_content_type("image/jpeg") == ".jpg"
    assert backend._business_card_extension_for_content_type("image/png") == ".png"
    assert backend._business_card_extension_for_content_type("image/webp") == ".webp"
    assert backend._business_card_extension_for_content_type("application/octet-stream") == ".img"


def test_business_card_s3_object_key_uses_prefix(monkeypatch):
    monkeypatch.setattr(backend, "ORG_BUSINESS_CARD_S3_PREFIX", "business-cards")
    key = backend._business_card_s3_object_key(
        submission_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        content_type="image/jpeg",
        now=backend.datetime(2026, 4, 26, tzinfo=backend.timezone.utc),
    )
    assert key.startswith("business-cards/2026/04/")
    assert key.endswith(".jpg")


def test_resolve_business_card_storage_path_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "ORG_BUSINESS_CARD_STORAGE_DIR", str(tmp_path))
    try:
        backend._resolve_business_card_storage_path("../escape.jpg")
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400


def test_persist_business_card_image_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "ORG_BUSINESS_CARD_STORAGE_ENABLED", True)
    monkeypatch.setattr(backend, "ORG_BUSINESS_CARD_STORAGE_BACKEND", "local")
    monkeypatch.setattr(backend, "ORG_BUSINESS_CARD_STORAGE_DIR", str(tmp_path))
    submission_id = uuid.uuid4()
    storage_backend, storage_bucket, relative_path = backend._persist_business_card_image(
        submission_id=submission_id,
        image_bytes=b"abc123",
        content_type="image/png",
    )
    assert storage_backend == "local"
    assert storage_bucket is None
    full_path = backend._resolve_business_card_storage_path(relative_path)
    assert full_path.exists()
    assert full_path.read_bytes() == b"abc123"
