import uuid
from datetime import date, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Account,
    EconomicEngine,
    InsurancePolicy,
    InsurancePolicyCreate,
    Transaction,
    TransactionType,
    get_current_user,
    get_db,
)

router = APIRouter(tags=["insurance"])


@router.get("/api/insurance/policies", response_model=List[dict])
async def list_insurance_policies(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """List insurance policies for the current account."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    policies = (
        session.query(InsurancePolicy)
        .filter_by(account_id=account.id)
        .order_by(InsurancePolicy.start_date.desc())
        .all()
    )

    return [
        {
            "id": policy.id,
            "insurance_type": policy.insurance_type.value,
            "coverage_amount": policy.coverage_amount,
            "premium_amount": policy.premium_amount,
            "duration_years": policy.duration_years,
            "start_date": policy.start_date,
            "end_date": policy.end_date,
            "deductible": policy.deductible,
            "is_active": policy.is_active,
        }
        for policy in policies
    ]


@router.post("/api/insurance/policies", response_model=dict)
async def create_insurance_policy(
    policy_data: InsurancePolicyCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Purchase an insurance policy."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    risk_factors = {"age": 35, "health_score": 75, "location_risk": "medium"}
    premium = EconomicEngine.calculate_insurance_premium(
        policy_data.insurance_type,
        policy_data.coverage_amount,
        risk_factors,
    )

    if account.balance < premium:
        raise HTTPException(status_code=400, detail="Insufficient funds for premium")

    policy = InsurancePolicy(
        id=uuid.uuid4(),
        account_id=account.id,
        insurance_type=policy_data.insurance_type,
        coverage_amount=policy_data.coverage_amount,
        premium_amount=premium,
        duration_years=policy_data.duration_years,
        start_date=date.today(),
        end_date=date.today() + timedelta(days=policy_data.duration_years * 365),
        beneficiaries=policy_data.beneficiaries,
        deductible=policy_data.deductible,
    )

    account.balance -= premium

    transaction = Transaction(
        id=uuid.uuid4(),
        from_account_id=account.id,
        amount=premium,
        transaction_type=TransactionType.INSURANCE_PREMIUM,
        description=f"{policy_data.insurance_type.value} insurance premium",
    )

    session.add(policy)
    session.add(transaction)
    session.commit()

    return {
        "policy_id": policy.id,
        "premium": premium,
        "coverage_amount": policy_data.coverage_amount,
        "start_date": policy.start_date,
        "end_date": policy.end_date,
    }
