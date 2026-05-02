import json
import uuid
from datetime import datetime
from typing import Any, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Account,
    RecentTransactionResponse,
    Transaction,
    TransactionCreate,
    TransactionResponse,
    TransactionType,
    db,
    get_async_db,
    get_current_user,
    get_db,
    logger,
)

router = APIRouter()


@router.post("/api/transactions", response_model=TransactionResponse)
async def create_transaction(
    transaction_data: TransactionCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db),
):
    """Create a new financial transaction."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    sender = session.query(Account).filter_by(email=current_user["email"]).first()
    if not sender:
        raise HTTPException(status_code=404, detail="Sender account not found")

    recipient = None
    if transaction_data.to_account_id:
        recipient = session.query(Account).filter_by(id=transaction_data.to_account_id).first()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient account not found")

    if transaction_data.transaction_type not in [TransactionType.UBI_PAYMENT, TransactionType.GRANT]:
        if sender.balance < transaction_data.amount:
            raise HTTPException(status_code=400, detail="Insufficient funds")

    transaction_id = uuid.uuid4()

    try:
        if transaction_data.transaction_type not in [TransactionType.UBI_PAYMENT, TransactionType.GRANT]:
            await conn.execute(
                """
                UPDATE accounts
                SET balance = balance - $1, updated_at = NOW()
                WHERE id = $2 AND balance >= $1
                """,
                float(transaction_data.amount),
                sender.id,
            )

        if recipient:
            await conn.execute(
                """
                UPDATE accounts
                SET balance = balance + $1, updated_at = NOW()
                WHERE id = $2
                """,
                float(transaction_data.amount),
                recipient.id,
            )

        transaction = Transaction(
            id=transaction_id,
            from_account_id=sender.id,
            to_account_id=recipient.id if recipient else None,
            amount=transaction_data.amount,
            transaction_type=transaction_data.transaction_type,
            description=transaction_data.description,
            reference_id=transaction_data.reference_id,
            tx_metadata=transaction_data.metadata,
        )

        session.add(transaction)
        session.commit()
        session.refresh(transaction)

        cache_key = f"transaction:{transaction_id}"
        db.redis_client.setex(
            cache_key,
            300,
            json.dumps(
                {
                    "id": str(transaction.id),
                    "from_account_id": str(transaction.from_account_id) if transaction.from_account_id else None,
                    "to_account_id": str(transaction.to_account_id) if transaction.to_account_id else None,
                    "amount": str(transaction.amount),
                    "transaction_type": transaction.transaction_type.value,
                    "description": transaction.description,
                    "timestamp": transaction.timestamp.isoformat(),
                    "metadata": transaction.tx_metadata,
                }
            ),
        )

        return transaction

    except asyncpg.exceptions.CheckViolationError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Transaction failed: constraint violation")
    except Exception as e:
        session.rollback()
        logger.error(f"Transaction failed: {e}")
        raise HTTPException(status_code=500, detail="Transaction failed")


@router.get("/api/accounts/me/transactions", response_model=List[TransactionResponse])
async def get_my_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
):
    """Get current user's transaction history."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    transactions = (
        session.query(Transaction)
        .filter((Transaction.from_account_id == account.id) | (Transaction.to_account_id == account.id))
        .order_by(Transaction.timestamp.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return transactions


@router.get("/api/accounts/me/transactions/incoming", response_model=List[TransactionResponse])
async def get_my_incoming_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    since: Optional[datetime] = None,
    limit: int = 50,
):
    """Get incoming transactions only for current user, suitable for polling automation."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    safe_limit = max(1, min(limit, 500))
    query = session.query(Transaction).filter(Transaction.to_account_id == account.id)
    if since is not None:
        query = query.filter(Transaction.timestamp >= since)
    return query.order_by(Transaction.timestamp.desc()).limit(safe_limit).all()


@router.get("/api/transactions/recent", response_model=List[RecentTransactionResponse])
async def get_recent_transactions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 10,
):
    """Get most recent transactions across the org ledger."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_limit = max(1, min(limit, 100))
    txns = session.query(Transaction).order_by(Transaction.timestamp.desc()).limit(safe_limit).all()
    if not txns:
        return []

    account_ids: set[uuid.UUID] = set()
    for txn in txns:
        if txn.from_account_id:
            account_ids.add(txn.from_account_id)
        if txn.to_account_id:
            account_ids.add(txn.to_account_id)

    account_name_map: dict[uuid.UUID, str] = {}
    if account_ids:
        rows = session.query(Account.id, Account.name).filter(Account.id.in_(account_ids)).all()
        account_name_map = {row.id: row.name for row in rows}

    return [
        {
            "id": txn.id,
            "timestamp": txn.timestamp,
            "transaction_type": txn.transaction_type,
            "amount": txn.amount,
            "currency": txn.currency,
            "description": txn.description,
            "from_account_id": txn.from_account_id,
            "to_account_id": txn.to_account_id,
            "from_account_name": account_name_map.get(txn.from_account_id) if txn.from_account_id else None,
            "to_account_name": account_name_map.get(txn.to_account_id) if txn.to_account_id else None,
        }
        for txn in txns
    ]
