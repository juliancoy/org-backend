import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from org import (
    Department,
    DepartmentProgram,
    DepartmentProgramResponse,
    DepartmentResponse,
    TreasuryAccount,
    TreasuryAccountResponse,
    get_current_user,
    get_db,
)

router = APIRouter(tags=["departments"])


@router.get("/api/departments", response_model=List[DepartmentResponse])
async def list_departments(
    active: bool | None = True,
    session: Session = Depends(get_db),
):
    query = session.query(Department)
    if active is not None:
        query = query.filter(Department.active == active)
    rows = query.order_by(Department.name.asc()).all()
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "domain": row.domain,
            "mandate": row.mandate,
            "account_id": row.account_id,
            "balance": row.account.balance if row.account else 0,
            "active": row.active,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


@router.get("/api/departments/{department_code}", response_model=DepartmentResponse)
async def get_department(
    department_code: str,
    session: Session = Depends(get_db),
):
    row = session.query(Department).filter(Department.code == department_code).first()
    if not row:
        raise HTTPException(status_code=404, detail="Department not found")
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "domain": row.domain,
        "mandate": row.mandate,
        "account_id": row.account_id,
        "balance": row.account.balance if row.account else 0,
        "active": row.active,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/api/departments/{department_id}/programs", response_model=List[DepartmentProgramResponse])
async def list_department_programs(
    department_id: uuid.UUID,
    active: bool | None = True,
    session: Session = Depends(get_db),
):
    query = session.query(DepartmentProgram).filter(DepartmentProgram.department_id == department_id)
    if active is not None:
        query = query.filter(DepartmentProgram.active == active)
    rows = query.order_by(DepartmentProgram.name.asc()).all()
    return [
        {
            "id": row.id,
            "department_id": row.department_id,
            "code": row.code,
            "name": row.name,
            "mandate": row.mandate,
            "account_id": row.account_id,
            "balance": row.account.balance if row.account else None,
            "active": row.active,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


@router.get("/api/treasury/accounts", response_model=List[TreasuryAccountResponse])
async def list_treasury_accounts(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    rows = session.query(TreasuryAccount).order_by(TreasuryAccount.name.asc()).all()
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "account_id": row.account_id,
            "balance": row.account.balance if row.account else 0,
            "purpose": row.purpose,
            "active": row.active,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]
