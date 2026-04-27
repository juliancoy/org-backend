import importlib.util
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_is_sysadmin_reads_sysadmin_flag():
    assert backend._is_sysadmin({"is_sysadmin": True}) is True
    assert backend._is_sysadmin({"is_sysadmin": False}) is False


def test_resolve_access_classes_for_anonymous_user():
    snapshot = backend._resolve_access_classes(session=object(), current_user={"is_anonymous": True})
    assert snapshot.is_public is True
    assert snapshot.is_sysadmin is False
    assert snapshot.is_org_admin is False
    assert snapshot.is_member is False
    assert snapshot.is_attendee is False


def test_resolve_access_classes_for_authenticated_public_user(monkeypatch):
    monkeypatch.setattr(backend, "_is_sysadmin", lambda _: False)
    monkeypatch.setattr(backend, "_is_any_org_admin", lambda _s, _u: False)
    monkeypatch.setattr(backend, "_has_active_team_membership", lambda _s, _uid: False)
    monkeypatch.setattr(backend, "_has_recent_attendance", lambda _s, _uid, days=90: False)

    snapshot = backend._resolve_access_classes(
        session=object(),
        current_user={"is_anonymous": False, "pidp_id": "user-1"},
    )
    assert snapshot.is_public is True
    assert snapshot.is_sysadmin is False
    assert snapshot.is_org_admin is False
    assert snapshot.is_member is False
    assert snapshot.is_attendee is False


def test_resolve_access_classes_for_member_attendee_org_admin(monkeypatch):
    monkeypatch.setattr(backend, "_is_sysadmin", lambda _: False)
    monkeypatch.setattr(backend, "_is_any_org_admin", lambda _s, _u: True)
    monkeypatch.setattr(backend, "_has_active_team_membership", lambda _s, _uid: True)
    monkeypatch.setattr(backend, "_has_recent_attendance", lambda _s, _uid, days=90: True)

    snapshot = backend._resolve_access_classes(
        session=object(),
        current_user={"is_anonymous": False, "pidp_id": "user-2"},
    )
    assert snapshot.is_public is True
    assert snapshot.is_org_admin is True
    assert snapshot.is_member is True
    assert snapshot.is_attendee is True
    assert snapshot.is_sysadmin is False


def test_resolve_access_classes_for_sysadmin(monkeypatch):
    monkeypatch.setattr(backend, "_is_sysadmin", lambda _: True)
    monkeypatch.setattr(backend, "_is_any_org_admin", lambda _s, _u: True)
    monkeypatch.setattr(backend, "_has_active_team_membership", lambda _s, _uid: False)
    monkeypatch.setattr(backend, "_has_recent_attendance", lambda _s, _uid, days=90: False)

    snapshot = backend._resolve_access_classes(
        session=object(),
        current_user={"is_anonymous": False, "pidp_id": "sys-1"},
    )
    assert snapshot.is_sysadmin is True
    assert snapshot.is_org_admin is True
    assert snapshot.is_member is True


def test_has_required_pat_grant_accepts_wildcard_and_exact():
    pat_user = {
        "token_kind": "pat",
        "token_scope_grants": ["org:admin.read", "org:*"],
    }
    assert backend._has_required_pat_grant(pat_user, ["org:admin.read"]) is True
    assert backend._has_required_pat_grant(pat_user, ["org:admin.write"]) is True


def test_has_required_pat_grant_rejects_missing_grant_for_pat():
    pat_user = {
        "token_kind": "pat",
        "token_scope_grants": ["org:profile.read"],
    }
    assert backend._has_required_pat_grant(pat_user, ["org:admin.read"]) is False


def test_has_required_pat_grant_does_not_block_jwt_sessions():
    jwt_user = {
        "token_kind": "jwt",
        "token_scope_grants": [],
    }
    assert backend._has_required_pat_grant(jwt_user, ["org:admin.read"]) is True


def test_require_sysadmin_blocks_missing_pat_grants():
    user = {
        "is_sysadmin": True,
        "token_kind": "pat",
        "token_scope_grants": ["org:profile.read"],
    }
    try:
        backend._require_sysadmin(user, pat_required_grants=["org:admin.write", "org:*"])
    except backend.HTTPException as exc:
        assert exc.status_code == 403
        assert "required grant" in str(exc.detail).lower()
    else:
        raise AssertionError("Expected HTTPException for missing PAT grant")


def test_can_use_sysadmin_override_requires_sysadmin_and_grant_for_pat():
    pat_user_without_grant = {
        "is_sysadmin": True,
        "token_kind": "pat",
        "token_scope_grants": ["org:profile.read"],
    }
    assert backend._can_use_sysadmin_override(pat_user_without_grant, ["org:admin.write", "org:*"]) is False

    pat_user_with_grant = {
        "is_sysadmin": True,
        "token_kind": "pat",
        "token_scope_grants": ["org:admin.write"],
    }
    assert backend._can_use_sysadmin_override(pat_user_with_grant, ["org:admin.write", "org:*"]) is True

    jwt_sysadmin_user = {
        "is_sysadmin": True,
        "token_kind": "jwt",
        "token_scope_grants": [],
    }
    assert backend._can_use_sysadmin_override(jwt_sysadmin_user, ["org:admin.write", "org:*"]) is True
