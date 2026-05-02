import uuid
from decimal import Decimal
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Account,
    EntityType,
    OrderStatus,
    OrderType,
    PortfolioHolding,
    Stock,
    StockCreate,
    StockOrderCreate,
    _is_sysadmin,
    get_async_db,
    get_current_user,
    get_db,
    is_market_open,
    logger,
)

router = APIRouter(tags=["stocks"])


@router.post("/api/stocks", response_model=dict)
async def create_stock(
    stock_data: StockCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """Create a new publicly traded company (admin/business only)."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if not _is_sysadmin(current_user) and (account.entity_type != EntityType.BUSINESS or not account.is_verified):
        raise HTTPException(status_code=403, detail="Only verified businesses can issue stocks")

    existing = session.query(Stock).filter_by(ticker_symbol=stock_data.ticker_symbol).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ticker symbol already exists")

    stock = Stock(
        id=uuid.uuid4(),
        company_name=stock_data.company_name,
        ticker_symbol=stock_data.ticker_symbol,
        current_price=stock_data.initial_price,
        day_open=stock_data.initial_price,
        day_high=stock_data.initial_price,
        day_low=stock_data.initial_price,
        total_shares=stock_data.total_shares,
        shares_outstanding=stock_data.total_shares,
        market_cap=stock_data.initial_price * stock_data.total_shares,
        sector=stock_data.sector,
        description=stock_data.description,
    )

    session.add(stock)
    session.commit()

    holding = PortfolioHolding(
        id=uuid.uuid4(),
        account_id=account.id,
        stock_id=stock.id,
        quantity=stock_data.total_shares,
        average_purchase_price=stock_data.initial_price,
        total_invested=stock_data.initial_price * stock_data.total_shares,
    )

    session.add(holding)
    session.commit()

    return {"stock_id": stock.id, "message": "Stock created successfully"}


@router.get("/api/stocks", response_model=List[dict])
async def list_stocks(
    session: Session = Depends(get_db),
    sector: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
):
    """List all available stocks."""
    query = session.query(Stock).filter_by(is_active=True)

    if sector:
        query = query.filter_by(sector=sector)

    stocks = query.order_by(Stock.market_cap.desc()).offset(skip).limit(limit).all()

    return [
        {
            "id": stock.id,
            "company_name": stock.company_name,
            "ticker_symbol": stock.ticker_symbol,
            "current_price": stock.current_price,
            "day_change": ((stock.current_price - stock.day_open) / stock.day_open * 100) if stock.day_open > 0 else 0,
            "volume": stock.volume,
            "market_cap": stock.market_cap,
            "sector": stock.sector,
        }
        for stock in stocks
    ]


@router.post("/api/stocks/orders")
async def place_stock_order(
    order_data: StockOrderCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    conn: asyncpg.Connection = Depends(get_async_db),
):
    """Place a stock market order."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")

    if not is_market_open():
        raise HTTPException(status_code=400, detail="Market is closed")

    account = session.query(Account).filter_by(email=current_user["email"]).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    stock = session.query(Stock).filter_by(id=order_data.stock_id).first()
    if not stock or not stock.is_active:
        raise HTTPException(status_code=404, detail="Stock not found or inactive")

    order_price = order_data.limit_price if order_data.order_type == OrderType.LIMIT else stock.current_price
    total_cost = order_price * order_data.quantity if order_data.action == "buy" else Decimal("0.00")

    try:
        async with conn.transaction():
            if order_data.action == "buy":
                if account.balance < total_cost:
                    raise HTTPException(status_code=400, detail="Insufficient funds")

                await conn.execute(
                    """
                    UPDATE accounts
                    SET balance = balance - $1, updated_at = NOW()
                    WHERE id = $2 AND balance >= $1
                    """,
                    float(total_cost),
                    account.id,
                )

            else:
                holding = await conn.fetchrow(
                    """
                    SELECT quantity FROM portfolio_holdings
                    WHERE account_id = $1 AND stock_id = $2
                    """,
                    account.id,
                    stock.id,
                )

                if not holding or holding["quantity"] < order_data.quantity:
                    raise HTTPException(status_code=400, detail="Insufficient shares")

            order_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO stock_orders
                (id, account_id, stock_id, order_type, action, quantity, limit_price, status, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                """,
                order_id,
                account.id,
                stock.id,
                order_data.order_type.value,
                order_data.action,
                order_data.quantity,
                float(order_data.limit_price) if order_data.limit_price else None,
                OrderStatus.PENDING.value,
            )

            await _match_order(conn, order_id, stock, order_data.action)

            return {"order_id": order_id, "status": "placed"}

    except asyncpg.exceptions.CheckViolationError:
        raise HTTPException(status_code=400, detail="Order placement failed")
    except Exception as e:
        logger.error(f"Order placement failed: {e}")
        raise HTTPException(status_code=500, detail="Order placement failed")


async def _match_order(conn: asyncpg.Connection, order_id: uuid.UUID, stock: Stock, action: str) -> None:
    await conn.execute(
        """
        UPDATE stock_orders
        SET status = $1, executed_price = $2, executed_quantity = quantity, executed_at = NOW()
        WHERE id = $3
        """,
        OrderStatus.EXECUTED.value,
        float(stock.current_price),
        order_id,
    )

    price_impact = Decimal("0.001") * Decimal(stock.volume / max(stock.total_shares, 1))
    if action == "buy":
        new_price = stock.current_price * (Decimal("1.0") + price_impact)
    else:
        new_price = stock.current_price * (Decimal("1.0") - price_impact)

    await conn.execute(
        """
        UPDATE stocks
        SET current_price = $1,
            day_high = GREATEST(day_high, $1),
            day_low = LEAST(day_low, $1),
            volume = volume + 1,
            last_updated = NOW()
        WHERE id = $2
        """,
        float(new_price),
        stock.id,
    )
