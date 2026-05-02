import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from fastapi import APIRouter

router = APIRouter(tags=["network-chat"])

@router.post("/api/network/chat/bootstrap", response_model=MatrixBootstrapSessionResponse)
async def bootstrap_chat_session(
    current_user: dict = Depends(get_current_user),
):
    _require_authenticated_user(current_user)
    return await _bootstrap_matrix_session_for_current_user(current_user)


@router.get("/api/network/chat/rooms", response_model=List[OrgChatRoomDirectoryItemResponse])
async def list_org_chat_rooms_for_current_user(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        return []

    status_by_org_id: dict[uuid.UUID, str] = {}

    attendee_rows = (
        session.query(NetworkEvent.host_org_id)
        .join(EventAttendance, EventAttendance.event_id == NetworkEvent.id)
        .filter(
            EventAttendance.user_id == user_id,
            NetworkEvent.host_org_id.isnot(None),
        )
        .distinct()
        .all()
    )
    for (org_id,) in attendee_rows:
        if not org_id:
            continue
        status_by_org_id[org_id] = "attendee"

    member_rows = (
        session.query(OrganizationMembership.organization_id, OrganizationMembership.role)
        .filter(OrganizationMembership.user_id == user_id)
        .all()
    )
    for org_id, role in member_rows:
        if not org_id:
            continue
        normalized_role = str(role or "").strip().lower()
        existing = status_by_org_id.get(org_id)
        if normalized_role == "admin":
            status_by_org_id[org_id] = "admin"
        elif existing != "admin":
            status_by_org_id[org_id] = "member"

    claimed_rows = (
        session.query(Organization.id)
        .filter(Organization.claimed_by_user_id == user_id)
        .all()
    )
    for (org_id,) in claimed_rows:
        if not org_id:
            continue
        status_by_org_id[org_id] = "admin"

    if not status_by_org_id:
        return []

    org_ids = list(status_by_org_id.keys())
    membership_count_rows = (
        session.query(
            OrganizationMembership.organization_id.label("organization_id"),
            func.count(OrganizationMembership.user_id).label("membership_count"),
        )
        .filter(OrganizationMembership.organization_id.in_(org_ids))
        .group_by(OrganizationMembership.organization_id)
        .all()
    )
    membership_count_by_org_id = {
        organization_id: int(membership_count or 0)
        for organization_id, membership_count in membership_count_rows
    }
    organizations = (
        session.query(Organization.id, Organization.name, Organization.slug)
        .filter(Organization.id.in_(org_ids))
        .all()
    )

    timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
    response_items: list[OrgChatRoomDirectoryItemResponse] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for org in organizations:
            relationship_status = status_by_org_id.get(org.id, "attendee")
            room_id: Optional[str] = None
            room_alias: Optional[str] = None
            room_name: Optional[str] = org.name
            try:
                ensured = await _matrix_ensure_org_chat_rooms(
                    client=client,
                    org_name=org.name,
                    org_slug=org.slug,
                    allow_create=(
                        ORG_MATRIX_AUTO_PROVISION_PUBLIC_ORG_ROOMS
                        and relationship_status in {"member", "admin"}
                    ),
                )
                public_room = ensured.get("public_chat") or {}
                room_id = str(public_room.get("room_id") or "").strip() or None
                room_alias = str(public_room.get("room_alias") or "").strip() or None
                room_name = str(public_room.get("room_name") or "").strip() or org.name
            except Exception as exc:
                logger.warning("Matrix room ensure skipped for org_slug=%s error=%s", org.slug, exc)
            if not room_id:
                continue
            response_items.append(
                OrgChatRoomDirectoryItemResponse(
                    organization_id=org.id,
                    organization_name=org.name,
                    organization_slug=org.slug,
                    relationship_status=relationship_status,
                    organization_member_count=membership_count_by_org_id.get(org.id, 0),
                    room_id=room_id,
                    room_alias=room_alias,
                    room_name=room_name,
                )
            )

    response_items.sort(
        key=lambda item: (
            -int(item.organization_member_count or 0),
            item.organization_name.lower(),
        )
    )
    return response_items


@router.post("/api/network/chat/rooms/backfill", response_model=OrgChatRoomBackfillResponse)
async def backfill_org_chat_rooms(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.write", "org:*"])
    organizations = session.query(Organization.id, Organization.name, Organization.slug).order_by(Organization.slug.asc()).all()
    result = OrgChatRoomBackfillResponse(
        organizations_total=len(organizations),
        organizations_scanned=0,
        public_rooms_found=0,
        public_rooms_created=0,
        announcements_rooms_found=0,
        announcements_rooms_created=0,
        errors=[],
    )
    if not organizations:
        return result

    timeout = httpx.Timeout(connect=8.0, read=10.0, write=10.0, pool=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for org in organizations:
            result.organizations_scanned += 1
            try:
                ensured = await _matrix_ensure_org_chat_rooms(
                    client=client,
                    org_name=org.name,
                    org_slug=org.slug,
                    allow_create=True,
                )
                public_room = ensured.get("public_chat") or {}
                if str(public_room.get("room_id") or "").strip():
                    if bool(public_room.get("created")):
                        result.public_rooms_created += 1
                    else:
                        result.public_rooms_found += 1

                announcements_room = ensured.get("announcements") or {}
                if str(announcements_room.get("room_id") or "").strip():
                    if bool(announcements_room.get("created")):
                        result.announcements_rooms_created += 1
                    else:
                        result.announcements_rooms_found += 1
            except Exception as exc:
                result.errors.append(f"{org.slug}: {exc}")
    return result


@router.get("/api/network/chat/link-preview", response_model=ChatLinkPreviewResponse)
async def get_chat_link_preview(
    request: Request,
    url: str = Query(..., min_length=5, max_length=2048),
    current_user: dict = Depends(get_current_user),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user) or "unknown"
    _throttle_action(f"network:chat-link-preview:user:{user_id}", limit=120, window_seconds=3600)
    _throttle_action(f"network:chat-link-preview:ip:{_request_client_ip(request)}", limit=300, window_seconds=3600)
    return await _fetch_chat_link_preview(url)


@router.get("/api/network/orgs/{organization_id}/members", response_model=List[OrganizationMembershipResponse])
async def list_org_members(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    members = (
        session.query(OrganizationMembership)
        .filter(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.role.desc(), OrganizationMembership.user_name.asc())
        .all()
    )
    return members


@router.post("/api/network/orgs/{organization_id}/members", response_model=OrganizationMembershipResponse)
async def upsert_org_member(
    organization_id: uuid.UUID,
    payload: OrganizationMembershipUpsert,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _throttle_action(f"network:upsert-member:{_actor_user_id(current_user)}", limit=120, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == payload.user_id.strip(),
        )
        .first()
    )
    if not member:
        member = OrganizationMembership(
            id=uuid.uuid4(),
            organization_id=organization_id,
            user_id=payload.user_id.strip(),
        )
        session.add(member)

    member.user_email = payload.user_email
    member.user_name = payload.user_name
    member.role = payload.role
    member.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.upserted",
        target_type="organization_membership",
        target_id=f"{organization_id}:{member.user_id}",
        metadata={"organization_id": str(organization_id), "role": member.role},
    )
    session.commit()
    session.refresh(member)
    return member


@router.patch("/api/network/orgs/{organization_id}/members/{user_id}", response_model=OrganizationMembershipResponse)
async def update_org_member(
    organization_id: uuid.UUID,
    user_id: str,
    payload: OrganizationMembershipUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _throttle_action(f"network:update-member:{_actor_user_id(current_user)}", limit=120, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    member.role = payload.role
    member.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.role_updated",
        target_type="organization_membership",
        target_id=f"{organization_id}:{member.user_id}",
        metadata={"organization_id": str(organization_id), "role": member.role},
    )
    session.commit()
    session.refresh(member)
    return member


@router.delete("/api/network/orgs/{organization_id}/members/{user_id}")
async def delete_org_member(
    organization_id: uuid.UUID,
    user_id: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    member = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    session.delete(member)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.member.removed",
        target_type="organization_membership",
        target_id=f"{organization_id}:{user_id}",
        metadata={"organization_id": str(organization_id)},
    )
    session.commit()
    return {"ok": True}


@router.post("/api/network/orgs/{organization_id}/claim-requests", response_model=OrganizationClaimRequestResponse)
async def create_claim_request(
    organization_id: uuid.UUID,
    payload: OrganizationClaimRequestCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:create-claim-request:{user_id}", limit=20, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not org.claimed_by_user_id:
        raise HTTPException(status_code=400, detail="Organization is unclaimed. Use /claim endpoint.")
    if org.claimed_by_user_id == user_id:
        raise HTTPException(status_code=400, detail="You already own this organization.")

    existing_pending = (
        session.query(OrganizationClaimRequest)
        .filter(
            OrganizationClaimRequest.organization_id == organization_id,
            OrganizationClaimRequest.requested_by_user_id == user_id,
            OrganizationClaimRequest.status == "pending",
        )
        .first()
    )
    if existing_pending:
        return existing_pending

    claim = OrganizationClaimRequest(
        id=uuid.uuid4(),
        organization_id=organization_id,
        requested_by_user_id=user_id,
        requested_by_email=current_user.get("email"),
        requested_by_name=current_user.get("name"),
        message=payload.message,
        status="pending",
    )
    session.add(claim)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.created",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(organization_id)},
    )
    session.commit()
    session.refresh(claim)
    return claim


@router.get("/api/network/orgs/{organization_id}/claim-requests", response_model=List[OrganizationClaimRequestResponse])
async def list_claim_requests(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status"),
):
    _require_authenticated_user(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    query = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.organization_id == organization_id)
    if status_filter:
        query = query.filter(OrganizationClaimRequest.status == status_filter)
    return query.order_by(OrganizationClaimRequest.created_at.desc()).all()


@router.get("/api/network/claim-requests", response_model=List[OrganizationClaimRequestQueueItemResponse])
async def list_claim_requests_queue(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    status_filter: str = Query("pending", alias="status"),
    limit: int = 200,
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.read", "org:*"])

    normalized_status = (status_filter or "pending").strip().lower()
    if normalized_status not in {"pending", "approved", "rejected", "all"}:
        raise HTTPException(status_code=422, detail="status must be one of: pending, approved, rejected, all")
    safe_limit = max(1, min(limit, 1000))

    query = (
        session.query(OrganizationClaimRequest, Organization)
        .join(Organization, Organization.id == OrganizationClaimRequest.organization_id)
    )
    if normalized_status != "all":
        query = query.filter(OrganizationClaimRequest.status == normalized_status)

    rows = (
        query.order_by(OrganizationClaimRequest.created_at.desc(), OrganizationClaimRequest.id.desc())
        .limit(safe_limit)
        .all()
    )
    return [
        OrganizationClaimRequestQueueItemResponse(
            id=claim.id,
            organization_id=org.id,
            organization_name=org.name,
            organization_slug=org.slug,
            organization_claimed_by_user_id=org.claimed_by_user_id,
            requested_by_user_id=claim.requested_by_user_id,
            requested_by_email=claim.requested_by_email,
            requested_by_name=claim.requested_by_name,
            message=claim.message,
            status=claim.status,
            reviewed_by_user_id=claim.reviewed_by_user_id,
            reviewed_at=claim.reviewed_at,
            created_at=claim.created_at,
        )
        for claim, org in rows
    ]


@router.post("/api/network/claim-requests/{claim_request_id}/approve", response_model=OrganizationResponse)
async def approve_claim_request(
    claim_request_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    claim = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.id == claim_request_id).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    org = session.query(Organization).filter(Organization.id == claim.organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail="Claim request is no longer pending")

    org.claimed_by_user_id = claim.requested_by_user_id
    membership = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == claim.requested_by_user_id,
        )
        .first()
    )
    if not membership:
        session.add(
            OrganizationMembership(
                id=uuid.uuid4(),
                organization_id=org.id,
                user_id=claim.requested_by_user_id,
                user_email=claim.requested_by_email,
                user_name=claim.requested_by_name,
                role="admin",
            )
        )
    else:
        membership.role = "admin"
        membership.user_email = claim.requested_by_email
        membership.user_name = claim.requested_by_name
        membership.updated_at = datetime.now(timezone.utc)

    claim.status = "approved"
    claim.reviewed_by_user_id = _actor_user_id(current_user)
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.approved",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(org.id), "new_owner": claim.requested_by_user_id},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, _actor_user_id(current_user))


@router.post("/api/network/claim-requests/{claim_request_id}/reject", response_model=OrganizationClaimRequestResponse)
async def reject_claim_request(
    claim_request_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    claim = session.query(OrganizationClaimRequest).filter(OrganizationClaimRequest.id == claim_request_id).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    org = session.query(Organization).filter(Organization.id == claim.organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail="Claim request is no longer pending")

    claim.status = "rejected"
    claim.reviewed_by_user_id = _actor_user_id(current_user)
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.claim_request.rejected",
        target_type="organization_claim_request",
        target_id=str(claim.id),
        metadata={"organization_id": str(org.id)},
    )
    session.commit()
    session.refresh(claim)
    return claim


@router.get("/api/network/audit-events")
async def list_network_audit_events(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 200,
    event_type_prefix: Optional[str] = None,
    target_type: Optional[str] = None,
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.read", "org:*"])
    safe_limit = max(1, min(limit, 2000))
    query = session.query(NetworkAuditEvent)
    if event_type_prefix:
        query = query.filter(NetworkAuditEvent.event_type.ilike(f"{event_type_prefix.strip()}%"))
    if target_type:
        query = query.filter(NetworkAuditEvent.target_type == target_type.strip())
    rows = query.order_by(NetworkAuditEvent.created_at.desc()).limit(safe_limit).all()
    return [
        {
            "id": str(row.id),
            "actor_user_id": row.actor_user_id,
            "actor_email": row.actor_email,
            "event_type": row.event_type,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "metadata": row.metadata_json or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _default_contact_slug(current_user: dict) -> str:
    name = str(current_user.get("name") or "").strip()
    email = str(current_user.get("email") or "").strip()
    if name:
        return _slugify(name)
    if email and "@" in email:
        return _slugify(email.split("@", 1)[0])
    return _slugify(_actor_user_id(current_user) or "contact")


def _map_contact(contact: UserContactPage, request: Optional[Request]) -> ContactPageResponse:
    public_url = None
    if request:
        public_url = f"{str(request.base_url).rstrip('/')}/users/{contact.slug}"
    links = []
    for raw in (contact.links or []):
        if isinstance(raw, dict) and raw.get("label") and raw.get("url"):
            links.append(ContactLink(label=str(raw["label"]), url=str(raw["url"])))
    return ContactPageResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        enabled=bool(contact.enabled),
        headline=contact.headline,
        bio=contact.bio,
        photo_url=contact.photo_url,
        email_public=contact.email_public,
        phone_public=contact.phone_public,
        linkedin_url=contact.linkedin_url,
        github_url=contact.github_url,
        x_url=contact.x_url,
        website_url=contact.website_url,
        source_profile_url=contact.source_profile_url,
        source_profile_imported_at=contact.source_profile_imported_at,
        links=links,
        public_url=public_url,
        updated_at=contact.updated_at,
    )


def _map_public_user_profile(contact: UserContactPage, request: Optional[Request], session: Session) -> PublicUserProfileResponse:
    public_url = None
    if request:
        public_url = f"{str(request.base_url).rstrip('/')}/users/{contact.slug}"
    links: list[ContactLink] = []
    for raw in (contact.links or []):
        if isinstance(raw, dict) and raw.get("label") and raw.get("url"):
            links.append(ContactLink(label=str(raw["label"]), url=str(raw["url"])))

    now_utc = datetime.now(timezone.utc)
    upcoming_events_count = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id == contact.user_id,
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .count()
    )
    return PublicUserProfileResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        headline=contact.headline,
        bio=contact.bio,
        photo_url=contact.photo_url,
        email_public=contact.email_public,
        phone_public=contact.phone_public,
        linkedin_url=contact.linkedin_url,
        github_url=contact.github_url,
        x_url=contact.x_url,
        website_url=contact.website_url,
        links=links,
        public_url=public_url,
        upcoming_events_count=upcoming_events_count,
        updated_at=contact.updated_at,
    )


def _map_public_user_list_item(contact: UserContactPage, upcoming_events_count: int) -> PublicUserListItemResponse:
    return PublicUserListItemResponse(
        user_id=contact.user_id,
        user_name=contact.user_name or "User",
        slug=contact.slug,
        headline=contact.headline,
        photo_url=contact.photo_url,
        upcoming_events_count=int(upcoming_events_count or 0),
        updated_at=contact.updated_at,
    )


