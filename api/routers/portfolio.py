from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import Account, PortfolioHolding, Stock, get_current_user, get_db

router = APIRouter(tags=["portfolio"])


@router.get("/api/portfolio", response_model=dict)
async def get_portfolio(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Get user's investment portfolio."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    holdings = (
        session.query(PortfolioHolding, Stock)
        .join(Stock, PortfolioHolding.stock_id == Stock.id)
        .filter(PortfolioHolding.account_id == account.id, PortfolioHolding.quantity > 0)
        .all()
    )

    portfolio_value = Decimal("0.00")
    total_invested = Decimal("0.00")
    holdings_data = []

    for holding, stock in holdings:
        current_value = stock.current_price * holding.quantity
        portfolio_value += current_value
        total_invested += holding.total_invested or Decimal("0.00")

        holdings_data.append(
            {
                "stock_id": stock.id,
                "ticker_symbol": stock.ticker_symbol,
                "company_name": stock.company_name,
                "quantity": holding.quantity,
                "average_price": holding.average_purchase_price,
                "current_price": stock.current_price,
                "current_value": current_value,
                "unrealized_gain": current_value - (holding.total_invested or Decimal("0.00")),
            }
        )

    return {
        "account_id": account.id,
        "portfolio_value": portfolio_value,
        "total_invested": total_invested,
        "unrealized_gains": portfolio_value - total_invested,
        "holdings": holdings_data,
        "cash_balance": account.balance,
    }
