import asyncio
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_admin_me_returns_sysadmin_flag_true():
    response = asyncio.run(
        backend.get_admin_status({"is_anonymous": False, "is_sysadmin": True})
    )
    assert response == {"is_sysadmin": True}


def test_admin_me_returns_false_for_anonymous():
    response = asyncio.run(backend.get_admin_status({"is_anonymous": True}))
    assert response == {"is_sysadmin": False}
