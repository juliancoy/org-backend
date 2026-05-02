import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from fastapi import APIRouter

router = APIRouter(tags=["network-orgs"])

@router.post("/api/network/ingest/calendar", response_model=CalendarIngestResponse)
async def ingest_calendar_feed(
    payload: CalendarIngestPayload,
    request: Request,
    session: Session = Depends(get_db),
):
    _require_ingest_auth(request)
    _throttle_action("network:ingest:calendar", limit=120, window_seconds=3600)
    return _ingest_calendar_payload(session, payload)


@router.get("/api/network/seed", response_model=SeedOrganizationsResponse)
async def seed_organizations(
    force_update: bool = False,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.write", "org:*"])
    return _seed_organizations_from_event_sources(session, force_update=force_update)


@router.get("/api/network/orgs", response_model=List[OrganizationResponse])
async def list_organizations(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    mine: bool = False,
    only_unclaimed: bool = False,
    limit: int = 250,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    user_id = _actor_user_id(current_user)
    query = session.query(Organization).order_by(Organization.name.asc())
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Organization.name.ilike(needle)) | (Organization.slug.ilike(needle)))
    if only_unclaimed:
        query = query.filter(Organization.claimed_by_user_id.is_(None))
    safe_offset = max(0, min(offset, 100000))
    organizations = query.offset(safe_offset).limit(safe_limit).all()
    if mine and user_id:
        organizations = [
            org for org in organizations
            if org.claimed_by_user_id == user_id
            or any(m.user_id == user_id for m in org.memberships or [])
        ]
    return [_map_org(org, user_id) for org in organizations]


@router.get("/api/network/teams", response_model=List[TeamResponse])
async def list_teams(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    active_only: bool = True,
    limit: int = 200,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(Team)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Team.name.ilike(needle)) | (Team.slug.ilike(needle)))
    if active_only:
        query = query.filter(Team.status == "active")
    return query.order_by(Team.name.asc()).offset(safe_offset).limit(safe_limit).all()


@router.post("/api/network/teams", response_model=TeamResponse)
async def create_team(
    payload: TeamCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.write", "org:*"])
    team = Team(
        id=uuid.uuid4(),
        name=payload.name.strip(),
        slug=_ensure_unique_team_slug(session, payload.name.strip()),
        description=(payload.description or "").strip() or None,
        status="active",
        created_by_user_id=_actor_user_id(current_user),
    )
    session.add(team)
    session.commit()
    session.refresh(team)
    return team


@router.post("/api/network/teams/{team_id}/members", response_model=TeamMembershipResponse)
async def upsert_team_membership(
    team_id: uuid.UUID,
    payload: TeamMembershipUpsert,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(current_user, pat_required_grants=["org:admin.write", "org:*"])
    team = session.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    user_id = payload.user_id.strip()
    membership = (
        session.query(TeamMembership)
        .filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        membership = TeamMembership(
            id=uuid.uuid4(),
            team_id=team.id,
            user_id=user_id,
        )
        session.add(membership)
    membership.user_email = payload.user_email
    membership.user_name = payload.user_name
    membership.role = payload.role
    membership.active = payload.active
    session.commit()
    session.refresh(membership)
    return membership


@router.get("/api/network/orgs/public", response_model=List[PublicOrganizationListItemResponse])
async def list_public_organizations(
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 250,
    offset: int = 0,
    sort: str = Query("popular", pattern="^(popular|name|newest)$"),
):
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    membership_counts = (
        session.query(
            OrganizationMembership.organization_id.label("organization_id"),
            func.count(OrganizationMembership.user_id).label("membership_count"),
        )
        .group_by(OrganizationMembership.organization_id)
        .subquery()
    )
    upcoming_event_counts = (
        session.query(
            NetworkEvent.host_org_id.label("organization_id"),
            func.count(NetworkEvent.id).label("upcoming_events_count"),
        )
        .filter(
            NetworkEvent.host_org_id.isnot(None),
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .group_by(NetworkEvent.host_org_id)
        .subquery()
    )
    pending_claim_counts = (
        session.query(
            OrganizationClaimRequest.organization_id.label("organization_id"),
            func.count(OrganizationClaimRequest.id).label("pending_claim_requests_count"),
        )
        .filter(OrganizationClaimRequest.status == "pending")
        .group_by(OrganizationClaimRequest.organization_id)
        .subquery()
    )

    membership_count_col = func.coalesce(membership_counts.c.membership_count, 0)
    upcoming_events_count_col = func.coalesce(upcoming_event_counts.c.upcoming_events_count, 0)
    pending_claim_requests_count_col = func.coalesce(pending_claim_counts.c.pending_claim_requests_count, 0)
    query = (
        session.query(Organization, membership_count_col, upcoming_events_count_col, pending_claim_requests_count_col)
        .outerjoin(membership_counts, membership_counts.c.organization_id == Organization.id)
        .outerjoin(upcoming_event_counts, upcoming_event_counts.c.organization_id == Organization.id)
        .outerjoin(pending_claim_counts, pending_claim_counts.c.organization_id == Organization.id)
    )
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter((Organization.name.ilike(needle)) | (Organization.slug.ilike(needle)))

    if sort == "name":
        query = query.order_by(Organization.name.asc())
    elif sort == "newest":
        query = query.order_by(Organization.created_at.desc(), Organization.name.asc())
    else:
        query = query.order_by(
            membership_count_col.desc(),
            upcoming_events_count_col.desc(),
            Organization.name.asc(),
        )

    rows = query.offset(safe_offset).limit(safe_limit).all()
    return [
        _map_public_org_list_item(
            org=org,
            membership_count=membership_count,
            upcoming_events_count=upcoming_events_count,
            pending_claim_requests_count=pending_claim_requests_count,
        )
        for org, membership_count, upcoming_events_count, pending_claim_requests_count in rows
    ]


@router.get("/api/network/orgs/public/{slug}", response_model=PublicOrganizationResponse)
async def get_public_organization(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        merged_redirect = (
            session.query(NetworkAuditEvent)
            .filter(
                NetworkAuditEvent.event_type == "org.merged",
                NetworkAuditEvent.metadata_json["source_slug"].astext == normalized,
            )
            .order_by(NetworkAuditEvent.created_at.desc())
            .first()
        )
        target_slug = None
        if merged_redirect and isinstance(merged_redirect.metadata_json, dict):
            target_slug = str(merged_redirect.metadata_json.get("target_slug") or "").strip().lower() or None
        if target_slug:
            redirected_org = session.query(Organization).filter(Organization.slug == target_slug).first()
            if redirected_org:
                try:
                    timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        await _matrix_ensure_org_chat_rooms(
                            client=client,
                            org_name=redirected_org.name,
                            org_slug=redirected_org.slug,
                            allow_create=True,
                        )
                except Exception as exc:
                    logger.warning("Matrix room ensure skipped for org_slug=%s error=%s", redirected_org.slug, exc)
                return _map_public_org(redirected_org, session, redirected_from_slug=normalized)
        raise HTTPException(status_code=404, detail="Organization not found")
    try:
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await _matrix_ensure_org_chat_rooms(
                client=client,
                org_name=org.name,
                org_slug=org.slug,
                allow_create=True,
            )
    except Exception as exc:
        logger.warning("Matrix room ensure skipped for org_slug=%s error=%s", org.slug, exc)
    return _map_public_org(org, session)


@router.get("/api/network/orgs/public/{slug}/admins", response_model=List[PublicOrganizationAdminResponse])
async def list_public_organization_admins(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    admins: list[PublicOrganizationAdminResponse] = []
    seen_user_ids: set[str] = set()
    for member in org.memberships or []:
        if member.role != "admin":
            continue
        seen_user_ids.add(member.user_id)
        admins.append(
            PublicOrganizationAdminResponse(
                user_id=member.user_id,
                user_name=member.user_name,
                user_email=member.user_email,
                role="admin",
            )
        )

    if org.claimed_by_user_id and org.claimed_by_user_id not in seen_user_ids:
        admins.append(
            PublicOrganizationAdminResponse(
                user_id=org.claimed_by_user_id,
                user_name=None,
                user_email=None,
                role="owner",
            )
        )

    return admins


@router.get("/api/network/orgs/public/{slug}/chat", response_model=PublicOrganizationChatResponse)
async def get_public_organization_chat(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = None
    timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            room_id, room_alias = await _resolve_org_public_chat_room(client, org.slug)
            room_name = org.name
            if not room_id:
                discovered_room_id, discovered_alias, discovered_name = await _matrix_find_public_room_for_org(
                    client,
                    slug=org.slug,
                    name=org.name,
                    limit=120,
                )
                if discovered_room_id:
                    room_id = discovered_room_id
                    room_alias = discovered_alias
                    room_name = discovered_name or org.name
    except Exception as exc:
        logger.warning("Unable to resolve public org chat slug=%s error=%s", org.slug, exc)

    return PublicOrganizationChatResponse(
        organization_slug=org.slug,
        room_exists=bool(room_id),
        room_id=room_id,
        room_alias=room_alias,
        room_name=room_name,
    )


@router.get("/api/network/orgs/public/{slug}/chat-feed", response_model=PublicOrganizationChatFeedResponse)
async def get_public_organization_chat_feed(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    cache_key = org.slug
    now_ts = time.time()
    cached = _ORG_CHAT_FEED_CACHE.get(cache_key)
    if cached and cached[0] > now_ts:
        cached_payload = cached[1]
        if isinstance(cached_payload, PublicOrganizationChatFeedResponse):
            return cached_payload

    timeout = httpx.Timeout(connect=8.0, read=10.0, write=10.0, pool=8.0)
    rooms: list[PublicOrganizationChatRoomFeedResponse] = [
        PublicOrganizationChatRoomFeedResponse(key="public_chat", label="Public Chat"),
        PublicOrganizationChatRoomFeedResponse(key="announcements", label="Announcements"),
    ]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            ensured = await _matrix_ensure_org_chat_rooms(
                client=client,
                org_name=org.name,
                org_slug=org.slug,
                # Public organization fetch already attempts creation; keep feed fast.
                allow_create=False,
            )

            public_room = ensured.get("public_chat") or {}
            public_room_id = str(public_room.get("room_id") or "").strip() or None
            rooms[0].room_id = public_room_id
            rooms[0].room_alias = str(public_room.get("room_alias") or "").strip() or None
            rooms[0].room_name = str(public_room.get("room_name") or "").strip() or org.name
            if public_room_id:
                rooms[0].messages = await _matrix_recent_room_messages(client, public_room_id, limit=15)

            announcements_room = ensured.get("announcements") or {}
            announcements_room_id = str(announcements_room.get("room_id") or "").strip() or None
            rooms[1].room_id = announcements_room_id
            rooms[1].room_alias = str(announcements_room.get("room_alias") or "").strip() or None
            rooms[1].room_name = str(announcements_room.get("room_name") or "").strip() or "Announcements"
            if announcements_room_id:
                rooms[1].messages = await _matrix_recent_room_messages(client, announcements_room_id, limit=15)
    except Exception as exc:
        logger.warning("Unable to resolve public org chat feed slug=%s error=%s", org.slug, exc)
    response_payload = PublicOrganizationChatFeedResponse(
        organization_slug=org.slug,
        rooms=rooms,
    )
    _ORG_CHAT_FEED_CACHE[cache_key] = (now_ts + ORG_CHAT_FEED_CACHE_TTL_SECONDS, response_payload)
    return response_payload


@router.post("/api/network/orgs/public/{slug}/claim", response_model=OrganizationResponse)
async def claim_public_organization(
    slug: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-org:{user_id}", limit=20, window_seconds=3600)

    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    _claim_org_record(session, org, current_user)
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@router.get("/api/network/orgs/public/{slug}/events", response_model=List[NetworkEventResponse])
async def list_public_organization_events(
    slug: str,
    session: Session = Depends(get_db),
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    normalized = slug.strip().lower()
    org = session.query(Organization).filter(Organization.slug == normalized).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)
    query = (
        session.query(NetworkEvent)
        .filter(NetworkEvent.host_org_id == org.id)
        .order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    )
    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@router.post("/api/network/orgs", response_model=OrganizationResponse)
async def create_organization(
    payload: OrganizationCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    _throttle_action(f"network:create-org:{user_id}", limit=10, window_seconds=3600)

    source_url = _validate_public_url(payload.source_url, "source_url")
    if source_url:
        existing = _find_org_by_source_url(session, source_url)
        if existing:
            raise HTTPException(status_code=409, detail="Organization for this source URL already exists")

    slug = _ensure_unique_org_slug(session, payload.name)
    org = Organization(
        id=uuid.uuid4(),
        name=payload.name.strip(),
        slug=slug,
        description=payload.description,
        source_url=source_url,
        source_urls=[source_url] if source_url else [],
        image_url=_validate_public_url(payload.image_url, "image_url"),
        tags=payload.tags or [],
        seeded_from_events=False,
        claimed_by_user_id=user_id if payload.claim_on_create else None,
        created_by_user_id=user_id,
    )
    session.add(org)
    if payload.claim_on_create:
        membership = OrganizationMembership(
            id=uuid.uuid4(),
            organization=org,
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            role="admin",
        )
        session.add(membership)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.created",
        target_type="organization",
        target_id=str(org.id),
        metadata={"slug": org.slug, "name": org.name, "claim_on_create": payload.claim_on_create},
    )
    session.commit()
    session.refresh(org)
    try:
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await _matrix_ensure_org_chat_rooms(
                client=client,
                org_name=org.name,
                org_slug=org.slug,
                allow_create=True,
            )
    except Exception as exc:
        logger.warning("Matrix room ensure skipped after org create slug=%s error=%s", org.slug, exc)
    return _map_org(org, user_id)


@router.post("/api/network/orgs/{organization_id}/claim", response_model=OrganizationResponse)
async def claim_organization(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-org:{user_id}", limit=20, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    _claim_org_record(session, org, current_user)
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@router.patch("/api/network/orgs/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:update-org:{user_id}", limit=80, window_seconds=3600)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not _is_org_admin(org, current_user):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    changed_fields: List[str] = []

    if payload.name is not None:
        next_name = payload.name.strip()
        if not next_name:
            raise HTTPException(status_code=422, detail="Organization name cannot be empty")
        if next_name != org.name:
            org.name = next_name
            changed_fields.append("name")
    if payload.description is not None:
        next_description = payload.description.strip() or None
        if next_description != org.description:
            org.description = next_description
            changed_fields.append("description")
    if payload.image_url is not None:
        next_image_url = _validate_public_url(payload.image_url.strip() or None, "image_url")
        if next_image_url != org.image_url:
            org.image_url = next_image_url
            changed_fields.append("image_url")
    if payload.tags is not None:
        next_tags = sorted(set((payload.tags or [])))
        if next_tags != (org.tags or []):
            org.tags = next_tags
            changed_fields.append("tags")

    if not changed_fields:
        return _map_org(org, user_id)

    org.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="org.updated",
        target_type="organization",
        target_id=str(org.id),
        metadata={"changed_fields": changed_fields},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@router.post("/api/network/orgs/{organization_id}/unclaim", response_model=OrganizationResponse)
async def unclaim_organization(
    organization_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.claimed_by_user_id is None:
        return _map_org(org, user_id)
    if not _can_use_sysadmin_override(current_user, ["org:admin.write", "org:*"]) and org.claimed_by_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the claiming user or an admin can unclaim this organization")

    previous_owner = org.claimed_by_user_id
    org.claimed_by_user_id = None
    _audit_event(
        session,
        actor=current_user,
        event_type="org.unclaimed",
        target_type="organization",
        target_id=str(org.id),
        metadata={"previous_owner": previous_owner},
    )
    session.commit()
    session.refresh(org)
    return _map_org(org, user_id)


@router.post("/api/network/orgs/{organization_id}/merge", response_model=OrganizationResponse)
async def merge_organization(
    organization_id: uuid.UUID,
    payload: OrganizationMergeRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:merge-org:{user_id}", limit=40, window_seconds=3600)

    target_org = session.query(Organization).filter(Organization.id == organization_id).first()
    if not target_org:
        raise HTTPException(status_code=404, detail="Target organization not found")
    source_org = session.query(Organization).filter(Organization.id == payload.source_organization_id).first()
    if not source_org:
        raise HTTPException(status_code=404, detail="Source organization not found")
    if target_org.id == source_org.id:
        raise HTTPException(status_code=422, detail="Source and target organizations must be different")

    if not _is_org_admin(target_org, current_user):
        raise HTTPException(status_code=403, detail="Target organization admin access required")
    if not _can_manage_org_for_merge(source_org, current_user):
        raise HTTPException(status_code=403, detail="Source organization is claimed by another admin")

    # Merge source URLs and keep canonical target source_url stable.
    merged_source_urls = _org_source_urls(target_org)
    for url in _org_source_urls(source_org):
        if url not in merged_source_urls:
            merged_source_urls.append(url)
    _set_org_source_urls(target_org, merged_source_urls)

    # Merge descriptive fields without clobbering richer manual data.
    if not target_org.description and source_org.description:
        target_org.description = source_org.description
    if not target_org.image_url and source_org.image_url:
        target_org.image_url = source_org.image_url
    target_org.tags = sorted(set((target_org.tags or []) + (source_org.tags or [])))
    target_org.seeded_from_events = bool(target_org.seeded_from_events or source_org.seeded_from_events)
    if not target_org.created_by_user_id and source_org.created_by_user_id:
        target_org.created_by_user_id = source_org.created_by_user_id
    if not target_org.claimed_by_user_id and source_org.claimed_by_user_id:
        target_org.claimed_by_user_id = source_org.claimed_by_user_id

    # Move hosted events.
    source_events = session.query(NetworkEvent).filter(NetworkEvent.host_org_id == source_org.id).all()
    for event in source_events:
        event.host_org = target_org
        event.host_type = EventHostType.ORG.value
        event.host_user_id = None
        event.updated_at = datetime.now(timezone.utc)

    # Merge memberships, upgrading role to admin if either side is admin.
    target_members = {
        member.user_id: member
        for member in session.query(OrganizationMembership).filter(OrganizationMembership.organization_id == target_org.id).all()
    }
    source_members = session.query(OrganizationMembership).filter(OrganizationMembership.organization_id == source_org.id).all()
    for source_member in source_members:
        existing_member = target_members.get(source_member.user_id)
        if existing_member:
            if source_member.role == "admin":
                existing_member.role = "admin"
            if not existing_member.user_email and source_member.user_email:
                existing_member.user_email = source_member.user_email
            if not existing_member.user_name and source_member.user_name:
                existing_member.user_name = source_member.user_name
            existing_member.updated_at = datetime.now(timezone.utc)
            session.delete(source_member)
            continue

        source_member.organization_id = target_org.id
        source_member.updated_at = datetime.now(timezone.utc)
        target_members[source_member.user_id] = source_member

    previous_target_claimed_by = target_org.claimed_by_user_id
    _audit_event(
        session,
        actor=current_user,
        event_type="org.merged",
        target_type="organization",
        target_id=str(target_org.id),
        metadata={
            "source_organization_id": str(source_org.id),
            "source_slug": source_org.slug,
            "target_slug": target_org.slug,
            "events_reassigned": len(source_events),
            "target_claimed_by_before": previous_target_claimed_by,
            "target_claimed_by_after": target_org.claimed_by_user_id,
            "source_urls": _org_source_urls(source_org),
            "merged_source_urls": _org_source_urls(target_org),
        },
    )

    # Flush before delete so relationship rebinding is persisted and no hosted events
    # remain attached to source_org in this transaction.
    session.flush()
    remaining_source_events = (
        session.query(NetworkEvent.id)
        .filter(NetworkEvent.host_org_id == source_org.id)
        .limit(1)
        .all()
    )
    if remaining_source_events:
        raise HTTPException(
            status_code=409,
            detail="Organization merge blocked: source organization still has bound hosted events.",
        )

    session.delete(source_org)
