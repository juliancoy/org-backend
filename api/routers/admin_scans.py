import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from org import (
    BusinessCardSubmission,
    BusinessCardSubmissionResponse,
    _load_business_card_image_bytes,
    _require_sysadmin,
    get_current_user,
    get_db,
)

router = APIRouter()


@router.get("/api/admin/scans/{submission_id}/image")
@router.get("/api/admin/business-cards/{submission_id}/image")
async def get_business_card_submission_image(
    submission_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.read", "org:*"],
        detail="SysAdmin access required",
    )

    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission:
        raise HTTPException(status_code=404, detail="Business card submission not found")
    if not submission.image_storage_path:
        raise HTTPException(status_code=404, detail="Stored image not available")

    download_name = (submission.image_filename or f"{submission.id}").strip() or f"{submission.id}"
    image_bytes = _load_business_card_image_bytes(
        storage_backend=(submission.image_storage_backend or "local"),
        storage_bucket=submission.image_storage_bucket,
        storage_path=submission.image_storage_path,
    )
    return Response(
        content=image_bytes,
        media_type=submission.image_content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{download_name}"',
            "Cache-Control": "private, max-age=0, no-cache, no-store",
        },
    )


@router.get("/api/admin/scans", response_model=List[BusinessCardSubmissionResponse])
@router.get("/api/admin/business-cards", response_model=List[BusinessCardSubmissionResponse])
async def list_business_card_submissions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.read", "org:*"],
        detail="SysAdmin access required",
    )
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    rows = (
        session.query(BusinessCardSubmission)
        .order_by(BusinessCardSubmission.created_at.desc(), BusinessCardSubmission.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )
    return rows
