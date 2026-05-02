import json
import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Account,
    EconomicEngine,
    EntityType,
    FiscalProposal,
    FiscalProposalCreate,
    FiscalVote,
    FiscalVoteCreate,
    TaxEstimate,
    TaxRecord,
    TransactionType,
    db,
    get_async_db,
    get_current_user,
    get_db,
    logger,
)

router = APIRouter(tags=["fiscal"])


@router.post("/api/fiscal/proposals", response_model=dict)
async def create_fiscal_proposal(
    proposal_data: FiscalProposalCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if not account.is_verified and account.entity_type == EntityType.INDIVIDUAL:
        raise HTTPException(status_code=403, detail="Account must be verified to create proposals")

    proposal = FiscalProposal(
        id=uuid.uuid4(),
        title=proposal_data.title,
        description=proposal_data.description,
        policy_area=proposal_data.policy_area,
        proposed_budget=proposal_data.proposed_budget,
        duration_months=proposal_data.duration_months,
        expected_impact=proposal_data.expected_impact,
        created_by=account.id,
        voting_start=datetime.now(timezone.utc),
        voting_end=datetime.now(timezone.utc) + timedelta(days=proposal_data.voting_days),
        status="voting",
    )

    session.add(proposal)
    session.commit()

    cache_key = f"proposal:{proposal.id}"
    db.redis_client.setex(
        cache_key,
        3600,
        json.dumps(
            {
                "id": str(proposal.id),
                "title": proposal.title,
                "policy_area": proposal.policy_area.value,
                "proposed_budget": str(proposal.proposed_budget),
                "status": proposal.status,
                "voting_end": proposal.voting_end.isoformat(),
            }
        ),
    )

    return {"proposal_id": proposal.id, "status": "created"}


@router.post("/api/fiscal/proposals/{proposal_id}/vote")
async def vote_on_proposal(
    proposal_id: uuid.UUID,
    vote_data: FiscalVoteCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    proposal = session.query(FiscalProposal).filter_by(id=proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.status != "voting" or datetime.now(timezone.utc) > proposal.voting_end:
        raise HTTPException(status_code=400, detail="Voting is closed")

    existing_vote = session.query(FiscalVote).filter_by(proposal_id=proposal_id, account_id=account.id).first()
    if existing_vote:
        raise HTTPException(status_code=400, detail="Already voted on this proposal")

    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO fiscal_votes (id, proposal_id, account_id, vote, rationale, timestamp)
                VALUES ($1, $2, $3, $4, $5, NOW())
                """,
                uuid.uuid4(),
                proposal_id,
                account.id,
                vote_data.vote.value,
                vote_data.rationale,
            )

            await conn.execute(
                f"""
                UPDATE fiscal_proposals
                SET {vote_data.vote.value}_votes = {vote_data.vote.value}_votes + 1,
                    total_votes = total_votes + 1,
                    updated_at = NOW()
                WHERE id = $1
                """,
                proposal_id,
            )

        return {"status": "vote_recorded", "vote": vote_data.vote}

    except Exception as e:
        logger.error(f"Vote failed: {e}")
        raise HTTPException(status_code=500, detail="Vote failed")


@router.post("/api/tax/calculate")
async def calculate_tax(
    tax_data: TaxEstimate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    tax_amount = EconomicEngine.calculate_tax(tax_data.taxable_income, account.entity_type)

    existing = session.query(TaxRecord).filter_by(account_id=account.id, tax_year=tax_data.tax_year).first()
    if existing:
        return {
            "taxable_income": tax_data.taxable_income,
            "tax_amount": tax_amount,
            "already_paid": existing.paid_amount,
            "balance_due": tax_amount - existing.paid_amount,
            "due_date": existing.due_date,
        }

    tax_record = TaxRecord(
        id=uuid.uuid4(),
        account_id=account.id,
        tax_year=tax_data.tax_year,
        taxable_income=tax_data.taxable_income,
        tax_amount=tax_amount,
        due_date=date(tax_data.tax_year + 1, 4, 15),
    )

    session.add(tax_record)
    session.commit()

    return {
        "taxable_income": tax_data.taxable_income,
        "tax_amount": tax_amount,
        "due_date": tax_record.due_date,
        "record_id": tax_record.id,
    }


@router.post("/api/tax/pay")
async def pay_taxes(
    record_id: uuid.UUID,
    amount: Decimal,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    tax_record = session.query(TaxRecord).filter_by(id=record_id, account_id=account.id).first()
    if not tax_record:
        raise HTTPException(status_code=404, detail="Tax record not found")

    if amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Payment amount must be positive")

    if amount > tax_record.tax_amount - tax_record.paid_amount:
        raise HTTPException(status_code=400, detail="Payment exceeds tax due")

    if account.balance < amount:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    try:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE accounts
                SET balance = balance - $1, updated_at = NOW()
                WHERE id = $2 AND balance >= $1
                """,
                float(amount),
                account.id,
            )

            await conn.execute(
                """
                UPDATE tax_records
                SET paid_amount = paid_amount + $1,
                    status = CASE
                        WHEN paid_amount + $1 >= tax_amount THEN 'paid'
                        ELSE 'partial'
                    END,
                    paid_at = CASE
                        WHEN paid_amount + $1 >= tax_amount THEN NOW()
                        ELSE paid_at
                    END,
                    updated_at = NOW()
                WHERE id = $2
                """,
                float(amount),
                record_id,
            )

            await conn.execute(
                """
                INSERT INTO transactions
                (id, from_account_id, amount, transaction_type, description, timestamp)
                VALUES ($1, $2, $3, $4, $5, NOW())
                """,
                uuid.uuid4(),
                account.id,
                float(amount),
                TransactionType.TAX_PAYMENT.value,
                f"Tax payment for {tax_record.tax_year}",
            )

        return {"paid": amount, "remaining": tax_record.tax_amount - tax_record.paid_amount - amount}

    except Exception as e:
        logger.error(f"Tax payment failed: {e}")
        raise HTTPException(status_code=500, detail="Tax payment failed")
