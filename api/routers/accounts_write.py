import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import Account, AccountResponse, AccountUpdate, EditRequest, get_current_user, get_db

router = APIRouter()


@router.patch("/api/accounts/me", response_model=AccountResponse)
async def update_account(
    update_data: AccountUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Update current user's account information."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    edit_request = EditRequest(
        id=uuid.uuid4(),
        account_id=account.id,
        field_name="account_update",
        old_value=json.dumps(
            {
                "name": account.name,
                "address": account.address,
                "business_type": account.business_type,
                "mission_statement": account.mission_statement,
            }
        ),
        new_value=json.dumps(update_data.dict(exclude_unset=True)),
        status="pending",
        message="Account information update request",
    )
    session.add(edit_request)

    update_dict = update_data.dict(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(account, field, value)

    account.updated_at = datetime.now(timezone.utc)

    session.commit()
    session.refresh(account)

    return account
