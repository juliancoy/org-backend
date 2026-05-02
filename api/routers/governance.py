import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from org import (
    GovernanceComment,
    GovernanceCommentCreate,
    GovernanceCommentResponse,
    GovernanceDissolutionExecuteRequest,
    GovernanceDissolutionPlan,
    GovernanceDissolutionPlanResponse,
    GovernanceMotion,
    GovernanceMotionCreate,
    GovernanceMotionResponse,
    GovernanceMotionStatus,
    GovernanceMotionType,
    GovernanceProposerType,
    GovernanceReaction,
    GovernanceReactionResponse,
    GovernanceReactionType,
    GovernanceUserVoteResponse,
    GovernanceVote,
    GovernanceVoteCountsResponse,
    GovernanceVoteResultResponse,
    GovernanceMotionVoteCastRequest,
    Organization,
    _actor_user_id,
    _audit_event,
    _can_manage_governance_motion,
    _can_use_sysadmin_override,
    _ensure_governance_transition,
    _get_dissolution_plan,
    _governance_reaction_counts,
    _governance_vote_result,
    _is_org_admin,
    _map_governance_motion,
    _require_authenticated_user,
    _require_sysadmin,
    _throttle_action,
    _validate_dissolution_payload,
    get_current_user,
    get_db,
)

router = APIRouter(tags=["governance"])


def _governance_actor_name(current_user: dict) -> str:
    return (
        str(current_user.get("name") or "").strip()
        or str(current_user.get("email") or "").strip()
        or _actor_user_id(current_user)
        or "Unknown"
    )


def _get_governance_motion_or_404(session: Session, motion_id: uuid.UUID) -> GovernanceMotion:
    motion = session.query(GovernanceMotion).filter(GovernanceMotion.id == motion_id).first()
    if not motion:
        raise HTTPException(status_code=404, detail="Motion not found")
    return motion


@router.get("/api/governance/motions", response_model=List[GovernanceMotionResponse])
async def list_governance_motions(
    session: Session = Depends(get_db),
    search: str = Query("", alias="search"),
    status: Optional[List[str]] = Query(None, alias="status"),
    type: Optional[str] = Query(None, alias="type"),
    parent_motion_id: Optional[uuid.UUID] = Query(None, alias="parent_motion_id"),
):
    query = session.query(GovernanceMotion)
    needle = (search or "").strip()
    if needle:
        like = f"%{needle}%"
        query = query.filter(
            (GovernanceMotion.title.ilike(like))
            | (GovernanceMotion.body.ilike(like))
            | (GovernanceMotion.proposer_name.ilike(like))
        )
    if status:
        allowed_statuses = {
            GovernanceMotionStatus.PROPOSED.value,
            GovernanceMotionStatus.SECONDED.value,
            GovernanceMotionStatus.DISCUSSION.value,
            GovernanceMotionStatus.VOTING.value,
            GovernanceMotionStatus.PASSED.value,
            GovernanceMotionStatus.FAILED.value,
            GovernanceMotionStatus.TABLED.value,
            GovernanceMotionStatus.WITHDRAWN.value,
        }
        statuses = [item for item in status if item in allowed_statuses]
        if statuses:
            query = query.filter(GovernanceMotion.status.in_(statuses))
    if type:
        if type not in {
            GovernanceMotionType.MAIN.value,
            GovernanceMotionType.AMENDMENT.value,
            GovernanceMotionType.DISSOLUTION.value,
        }:
            raise HTTPException(status_code=422, detail="Invalid type filter")
        query = query.filter(GovernanceMotion.type == type)
    if parent_motion_id:
        query = query.filter(GovernanceMotion.parent_motion_id == parent_motion_id)

    rows = query.order_by(GovernanceMotion.created_at.desc()).all()
    return [_map_governance_motion(row) for row in rows]


@router.get("/api/governance/motions/{motion_id}", response_model=GovernanceMotionResponse)
async def get_governance_motion(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return _map_governance_motion(motion)


@router.get(
    "/api/governance/motions/{motion_id}/dissolution-plan",
    response_model=GovernanceDissolutionPlanResponse,
)
async def get_governance_dissolution_plan(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.type != GovernanceMotionType.DISSOLUTION.value:
        raise HTTPException(status_code=404, detail="No dissolution plan for this motion")
    plan = _get_dissolution_plan(session, motion.id)
    if not plan:
        raise HTTPException(status_code=404, detail="Dissolution plan not found")
    return plan


@router.post("/api/governance/motions", response_model=GovernanceMotionResponse)
async def create_governance_motion(
    payload: GovernanceMotionCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"governance:create-motion:{user_id}", limit=40, window_seconds=3600)

    if payload.type == GovernanceMotionType.AMENDMENT.value and not payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="parent_motion_id is required for amendments")
    if payload.type in {GovernanceMotionType.MAIN.value, GovernanceMotionType.DISSOLUTION.value} and payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="parent_motion_id is only valid for amendments")
    _validate_dissolution_payload(payload)

    if payload.parent_motion_id:
        _get_governance_motion_or_404(session, payload.parent_motion_id)

    proposer_name = _governance_actor_name(current_user)
    proposer_user_name = proposer_name
    proposer_org_name = None
    proposer_org_id = None

    if payload.proposer_type == GovernanceProposerType.ORG.value:
        if not payload.proposer_org_id:
            raise HTTPException(status_code=422, detail="proposer_org_id is required for org proposer type")
        org = session.query(Organization).filter(Organization.id == payload.proposer_org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if not _is_org_admin(org, current_user):
            raise HTTPException(status_code=403, detail="Organization admin access required")
        proposer_name = org.name
        proposer_org_name = org.name
        proposer_org_id = org.id

    motion = GovernanceMotion(
        id=uuid.uuid4(),
        type=payload.type,
        parent_motion_id=payload.parent_motion_id,
        title=payload.title.strip(),
        body=payload.body.strip(),
        proposed_body_diff=(payload.proposed_body_diff or "").strip() or None,
        status=GovernanceMotionStatus.PROPOSED.value,
        proposer_type=payload.proposer_type,
        proposer_user_id=user_id,
        proposer_name=proposer_name,
        proposer_user_name=proposer_user_name,
        proposer_org_id=proposer_org_id,
        proposer_org_name=proposer_org_name,
        quorum_required=int(payload.quorum_required),
    )
    session.add(motion)
    if payload.type == GovernanceMotionType.DISSOLUTION.value:
        session.add(
            GovernanceDissolutionPlan(
                id=uuid.uuid4(),
                motion_id=motion.id,
                asset_disposition=(payload.dissolution_asset_disposition or "").strip(),
                asset_recipient_name=(payload.dissolution_asset_recipient_name or "").strip(),
                asset_recipient_type=(payload.dissolution_asset_recipient_type or "").strip(),
                legal_compliance_notes=(payload.dissolution_legal_compliance_notes or "").strip() or None,
            )
        )
    _audit_event(
        session,
        actor=current_user,
        event_type="governance.motion.created",
        target_type="governance_motion",
        target_id=str(motion.id),
        metadata={
            "motion_type": motion.type,
            "proposer_type": motion.proposer_type,
            "proposer_org_id": str(motion.proposer_org_id) if motion.proposer_org_id else None,
            "is_dissolution": motion.type == GovernanceMotionType.DISSOLUTION.value,
        },
    )
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post("/api/governance/motions/{motion_id}/second", response_model=GovernanceMotionResponse)
async def second_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    if motion.status != GovernanceMotionStatus.PROPOSED.value:
        raise HTTPException(status_code=400, detail="Motion must be in proposed status to second")
    if motion.proposer_user_id == user_id:
        raise HTTPException(status_code=400, detail="Proposer cannot second their own motion")
    motion.seconder_id = user_id
    motion.seconder_name = _governance_actor_name(current_user)
    motion.status = GovernanceMotionStatus.DISCUSSION.value
    motion.discussion_deadline = datetime.now(timezone.utc) + timedelta(days=2)
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post("/api/governance/motions/{motion_id}/open-voting", response_model=GovernanceMotionResponse)
async def open_governance_motion_voting(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.DISCUSSION.value:
        raise HTTPException(status_code=400, detail="Motion must be in discussion status")
    _ensure_governance_transition(motion, GovernanceMotionStatus.VOTING.value)
    motion.status = GovernanceMotionStatus.VOTING.value
    motion.voting_deadline = datetime.now(timezone.utc) + timedelta(days=1)
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post("/api/governance/motions/{motion_id}/table", response_model=GovernanceMotionResponse)
async def table_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.DISCUSSION.value:
        raise HTTPException(status_code=400, detail="Motion must be in discussion status")
    _ensure_governance_transition(motion, GovernanceMotionStatus.TABLED.value)
    motion.status = GovernanceMotionStatus.TABLED.value
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post("/api/governance/motions/{motion_id}/withdraw", response_model=GovernanceMotionResponse)
async def withdraw_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    if motion.proposer_user_id != user_id and not _can_use_sysadmin_override(current_user, ["org:admin.write", "org:*"]):
        raise HTTPException(status_code=403, detail="Only the proposer or an admin can withdraw this motion")
    if motion.status != GovernanceMotionStatus.PROPOSED.value:
        raise HTTPException(status_code=400, detail="Only proposed motions can be withdrawn")
    _ensure_governance_transition(motion, GovernanceMotionStatus.WITHDRAWN.value)
    motion.status = GovernanceMotionStatus.WITHDRAWN.value
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post("/api/governance/motions/{motion_id}/resolve", response_model=GovernanceMotionResponse)
async def resolve_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if not _can_manage_governance_motion(motion, current_user, session):
        raise HTTPException(status_code=403, detail="Motion management access required")
    if motion.status != GovernanceMotionStatus.VOTING.value:
        raise HTTPException(status_code=400, detail="Motion must be in voting status")
    if motion.type == GovernanceMotionType.DISSOLUTION.value:
        plan = _get_dissolution_plan(session, motion.id)
        if not plan:
            raise HTTPException(
                status_code=422,
                detail="Dissolution motion cannot be resolved without an asset disposition plan",
            )
    result = _governance_vote_result(motion)
    next_status = GovernanceMotionStatus.PASSED.value if result["passed"] else GovernanceMotionStatus.FAILED.value
    _ensure_governance_transition(motion, next_status)
    motion.status = next_status
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.post(
    "/api/governance/motions/{motion_id}/execute-dissolution",
    response_model=GovernanceDissolutionPlanResponse,
)
async def execute_governance_dissolution(
    motion_id: uuid.UUID,
    payload: GovernanceDissolutionExecuteRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.write", "org:*"])
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.type != GovernanceMotionType.DISSOLUTION.value:
        raise HTTPException(status_code=422, detail="Motion is not a dissolution motion")
    if motion.status != GovernanceMotionStatus.PASSED.value:
        raise HTTPException(status_code=422, detail="Dissolution can only be executed after a passed motion")
    plan = _get_dissolution_plan(session, motion.id)
    if not plan:
        raise HTTPException(status_code=422, detail="Missing dissolution asset disposition plan")
    plan.executed_at = datetime.now(timezone.utc)
    plan.executed_by_user_id = _actor_user_id(current_user)
    plan.execution_notes = (payload.execution_notes or "").strip() or None
    plan.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="governance.dissolution.executed",
        target_type="governance_motion",
        target_id=str(motion.id),
        metadata={
            "asset_recipient_name": plan.asset_recipient_name,
            "asset_recipient_type": plan.asset_recipient_type,
        },
    )
    session.commit()
    session.refresh(plan)
    return plan


@router.post("/api/governance/motions/{motion_id}/votes", response_model=GovernanceMotionResponse)
async def cast_governance_motion_vote(
    motion_id: uuid.UUID,
    payload: GovernanceMotionVoteCastRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    if motion.status != GovernanceMotionStatus.VOTING.value:
        raise HTTPException(status_code=400, detail="Motion is not open for voting")
    user_id = _actor_user_id(current_user)
    existing = (
        session.query(GovernanceVote)
        .filter(
            GovernanceVote.motion_id == motion.id,
            GovernanceVote.voter_user_id == user_id,
        )
        .first()
    )
    if existing:
        existing.choice = payload.choice
        existing.voter_name = _governance_actor_name(current_user)
        existing.cast_at = datetime.now(timezone.utc)
    else:
        session.add(
            GovernanceVote(
                id=uuid.uuid4(),
                motion_id=motion.id,
                voter_user_id=user_id,
                voter_name=_governance_actor_name(current_user),
                choice=payload.choice,
            )
        )
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    return _map_governance_motion(motion)


@router.get("/api/governance/motions/{motion_id}/comments", response_model=List[GovernanceCommentResponse])
async def list_governance_motion_comments(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    _get_governance_motion_or_404(session, motion_id)
    rows = (
        session.query(GovernanceComment)
        .filter(GovernanceComment.motion_id == motion_id)
        .order_by(GovernanceComment.created_at.asc())
        .all()
    )
    return rows


@router.post("/api/governance/motions/{motion_id}/comments", response_model=GovernanceCommentResponse)
async def create_governance_motion_comment(
    motion_id: uuid.UUID,
    payload: GovernanceCommentCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _get_governance_motion_or_404(session, motion_id)
    row = GovernanceComment(
        id=uuid.uuid4(),
        motion_id=motion_id,
        author_id=_actor_user_id(current_user),
        author_name=_governance_actor_name(current_user),
        body=payload.body.strip(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _set_governance_reaction(
    motion: GovernanceMotion,
    current_user: dict,
    session: Session,
    direction: str,
) -> GovernanceReactionResponse:
    user_id = _actor_user_id(current_user)
    existing = (
        session.query(GovernanceReaction)
        .filter(
            GovernanceReaction.motion_id == motion.id,
            GovernanceReaction.user_id == user_id,
        )
        .first()
    )
    if existing and existing.direction == direction:
        session.delete(existing)
        user_vote = None
    elif existing:
        existing.direction = direction
        existing.updated_at = datetime.now(timezone.utc)
        user_vote = direction
    else:
        session.add(
            GovernanceReaction(
                id=uuid.uuid4(),
                motion_id=motion.id,
                user_id=user_id,
                direction=direction,
            )
        )
        user_vote = direction
    motion.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(motion)
    counts = _governance_reaction_counts(motion)
    return GovernanceReactionResponse(score=counts.score, user_vote=user_vote)


@router.post("/api/governance/motions/{motion_id}/upvote", response_model=GovernanceReactionResponse)
async def upvote_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    return _set_governance_reaction(motion, current_user, session, GovernanceReactionType.UP.value)


@router.post("/api/governance/motions/{motion_id}/downvote", response_model=GovernanceReactionResponse)
async def downvote_governance_motion(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    motion = _get_governance_motion_or_404(session, motion_id)
    return _set_governance_reaction(motion, current_user, session, GovernanceReactionType.DOWN.value)


@router.get("/api/governance/motions/{motion_id}/user-vote", response_model=GovernanceUserVoteResponse)
async def get_governance_motion_user_vote(
    motion_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _get_governance_motion_or_404(session, motion_id)
    user_id = _actor_user_id(current_user)
    row = (
        session.query(GovernanceReaction)
        .filter(
            GovernanceReaction.motion_id == motion_id,
            GovernanceReaction.user_id == user_id,
        )
        .first()
    )
    return GovernanceUserVoteResponse(user_vote=row.direction if row else None)


@router.get("/api/governance/motions/{motion_id}/vote-counts", response_model=GovernanceVoteCountsResponse)
async def get_governance_motion_vote_counts(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return _governance_reaction_counts(motion)


@router.get("/api/governance/motions/{motion_id}/results", response_model=GovernanceVoteResultResponse)
async def get_governance_motion_results(
    motion_id: uuid.UUID,
    session: Session = Depends(get_db),
):
    motion = _get_governance_motion_or_404(session, motion_id)
    return GovernanceVoteResultResponse(**_governance_vote_result(motion))
