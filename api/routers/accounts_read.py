import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from org import (
    SYSTEM_CURRENCY,
    Account,
    AccountAutomationResponse,
    AccountListItemResponse,
    AccountResponse,
    get_current_user,
    get_db,
)

router = APIRouter()


@router.get("/api/accounts/me", response_model=AccountResponse)
async def get_my_account(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Get current user's account."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return account


@router.get("/api/accounts/me/automation", response_model=AccountAutomationResponse)
async def get_my_account_automation(
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Automation-friendly account discovery endpoint for receive/payment workflows."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    base = str(request.base_url).rstrip("/")
    return {
        "account_id": account.id,
        "name": account.name,
        "email": account.email,
        "balance": account.balance,
        "currency": SYSTEM_CURRENCY,
        "account_endpoint": f"{base}/api/accounts/me",
        "incoming_transactions_endpoint": f"{base}/api/accounts/me/transactions/incoming?limit=50",
        "all_transactions_endpoint": f"{base}/api/accounts/me/transactions?limit=50",
        "send_payment_endpoint": f"{base}/api/transactions",
        "send_url_template": f"{base}/send?to={account.id}&amount={{amount}}",
        "updated_at": account.updated_at,
    }


@router.get("/api/accounts/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    """Get account by ID."""
    account = session.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return account


@router.get("/api/accounts", response_model=List[AccountListItemResponse])
async def list_accounts(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    sort: str = "balance_desc",
    limit: int = 500,
):
    """List accounts for directory/search views."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_limit = max(1, min(limit, 2000))
    query = session.query(Account)

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (Account.name.ilike(needle)) | (Account.email.ilike(needle))
        )

    if sort == "balance_asc":
        query = query.order_by(Account.balance.asc(), Account.name.asc())
    elif sort == "name_asc":
        query = query.order_by(Account.name.asc())
    elif sort == "name_desc":
        query = query.order_by(Account.name.desc())
    else:
        query = query.order_by(Account.balance.desc(), Account.name.asc())

    return query.limit(safe_limit).all()
