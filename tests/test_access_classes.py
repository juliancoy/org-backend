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
