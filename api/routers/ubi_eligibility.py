import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Account,
    EconomicEngine,
    UBIEligibility,
    get_current_user,
    get_db,
    get_system_metrics,
    process_ubi_payment,
)

router = APIRouter()


@router.get("/api/ubi/eligibility")
async def get_ubi_eligibility(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Check UBI eligibility and next payment."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    eligibility = session.query(UBIEligibility).filter_by(account_id=account.id).first()

    if not eligibility:
        return {
            "is_eligible": False,
            "reason": "Not enrolled in UBI system",
        }

    if date.today() >= eligibility.next_payment_date:
        system_metrics = await get_system_metrics()
        ubi_amount = EconomicEngine.calculate_ubi_amount(
            account.balance,
            system_metrics["average_balance"],
        )

        asyncio.create_task(process_ubi_payment(account.id, ubi_amount))

        return {
            "is_eligible": True,
            "payment_due": True,
            "estimated_amount": ubi_amount,
            "next_payment_date": eligibility.next_payment_date,
        }

    return {
        "is_eligible": eligibility.is_eligible,
        "payment_due": False,
        "next_payment_date": eligibility.next_payment_date,
        "last_payment_amount": eligibility.last_payment_amount,
        "total_payments_received": eligibility.total_payments_received,
    }
