from typing import Any, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func
from sqlalchemy.orm import Session

from org import (
    ORG_SYSADMIN_EMAILS,
    ORG_SYSADMIN_USER_IDS,
    PIDP_BASE_URL,
    AccessClassSnapshotResponse,
    Account,
    AccountListItemResponse,
    _is_sysadmin,
    _require_sysadmin,
    _resolve_access_classes,
    _spicedb_check_sysadmin,
    get_current_user,
    get_db,
    logger,
    security,
)

router = APIRouter()


@router.get("/api/authz/me", response_model=AccessClassSnapshotResponse)
async def get_access_class_snapshot(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Resolve constitutional access classes for the current user."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return _resolve_access_classes(session, current_user)


@router.get("/admin/me")
async def get_admin_status(current_user: dict = Depends(get_current_user)):
    """Check if current user is a platform sysadmin."""
    if current_user.get("is_anonymous"):
        return {"is_sysadmin": False}
    is_sysadmin = _is_sysadmin(current_user)
    return {"is_sysadmin": is_sysadmin}


@router.get("/api/admin/accounts", response_model=List[AccountListItemResponse])
async def list_admin_accounts(
    current_user: dict = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db),
):
    """List sysadmin accounts by resolving PIDP users and SpiceDB admin membership."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(current_user, pat_required_grants=["org:admin.read", "org:*"])
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    token = credentials.credentials
    candidate_emails: set[str] = set(ORG_SYSADMIN_EMAILS)
    current_email = str(current_user.get("email") or "").strip().lower()
    if current_email:
        candidate_emails.add(current_email)
    if not candidate_emails:
        return []

    pidp_users: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for email in sorted(candidate_emails):
                resp = await client.get(
                    f"{PIDP_BASE_URL}/auth/users",
                    params={"email": email},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if not resp.is_success:
                    raise HTTPException(status_code=resp.status_code, detail="Unable to resolve PIDP users")
                response_payload = resp.json()
                if isinstance(response_payload, list):
                    pidp_users.extend(response_payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to load PIDP users for admin list: {exc}")
        raise HTTPException(status_code=503, detail="Unable to load admin list")

    admin_emails: set[str] = set()
    for pidp_user in pidp_users:
        pidp_id = str(pidp_user.get("id") or "").strip()
        email = str(pidp_user.get("email") or "").strip().lower()
        if not pidp_id or not email:
            continue
        is_sysadmin = pidp_id in ORG_SYSADMIN_USER_IDS or await _spicedb_check_sysadmin(pidp_id)
        if is_sysadmin:
            admin_emails.add(email)

    if not admin_emails:
        return []

    admins = (
        session.query(Account)
        .filter(func.lower(Account.email).in_(admin_emails))
        .order_by(Account.balance.desc(), Account.name.asc())
        .all()
    )
    return admins
