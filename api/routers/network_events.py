import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from fastapi import APIRouter

router = APIRouter(tags=["network-events"])

@router.get("/api/network/events", response_model=List[NetworkEventResponse])
async def list_network_events(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    mine: bool = False,
    only_unclaimed: bool = False,
    host_type: Optional[str] = None,
    limit: int = 250,
    offset: int = 0,
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(NetworkEvent).order_by(NetworkEvent.starts_at.desc().nullslast(), NetworkEvent.created_at.desc())
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
        )
    if only_unclaimed:
        query = query.filter(NetworkEvent.claimed_by_user_id.is_(None))
    if host_type and host_type.strip():
        normalized_host_type = host_type.strip().lower()
        if normalized_host_type not in {
            EventHostType.UNCLAIMED.value,
            EventHostType.INDIVIDUAL.value,
            EventHostType.ORG.value,
        }:
            raise HTTPException(status_code=422, detail="Invalid host_type filter")
        query = query.filter(NetworkEvent.host_type == normalized_host_type)

    events = query.offset(safe_offset).limit(safe_limit).all()
    user_id = _actor_user_id(current_user)
    if mine and user_id:
        filtered: list[NetworkEvent] = []
        for event in events:
            if event.claimed_by_user_id == user_id:
                filtered.append(event)
                continue
            if event.host_type == EventHostType.INDIVIDUAL.value and event.host_user_id == user_id:
                filtered.append(event)
                continue
            if event.host_type == EventHostType.ORG.value and event.host_org_id:
                host_org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
                if host_org and _is_org_admin(host_org, current_user):
                    filtered.append(event)
        events = filtered

    return [_map_network_event(event, current_user, session) for event in events]


@router.get("/api/network/events/public", response_model=List[NetworkEventResponse])
async def list_public_network_events(
    session: Session = Depends(get_db),
    q: str = "",
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    query = session.query(NetworkEvent)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
            | (NetworkEvent.description.ilike(needle))
        )

    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )
    query = query.order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@router.get("/api/network/events/public.json", response_model=NetworkEventPublicFeedResponse)
async def list_public_network_events_json(
    session: Session = Depends(get_db),
    q: str = "",
    upcoming_only: bool = False,
):
    now_utc = datetime.now(timezone.utc)
    query = session.query(NetworkEvent)

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkEvent.title.ilike(needle))
            | (NetworkEvent.slug.ilike(needle))
            | (NetworkEvent.location.ilike(needle))
            | (NetworkEvent.description.ilike(needle))
        )

    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc()).all()
    mapped_events = [_map_network_event(event, None, session) for event in events]
    return NetworkEventPublicFeedResponse(
        generated_at=datetime.now(timezone.utc),
        total=len(mapped_events),
        events=mapped_events,
    )


@router.get("/api/network/events/public/{slug}", response_model=NetworkEventResponse)
async def get_public_network_event_by_slug(
    slug: str,
    session: Session = Depends(get_db),
):
    event = session.query(NetworkEvent).filter(NetworkEvent.slug == slug.strip().lower()).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if ORG_MATRIX_AUTO_PROVISION_PUBLIC_EVENT_ROOMS:
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await _matrix_ensure_event_chat_room(
                    client=client,
                    event_title=event.title,
                    event_slug=event.slug,
                    allow_create=True,
                )
        except Exception as exc:
            logger.warning("Matrix event room ensure skipped for event_slug=%s error=%s", event.slug, exc)
    return _map_network_event(event, None, session)


@router.get("/api/network/events/public/{slug}/chat", response_model=PublicEventChatResponse)
async def get_public_event_chat(
    slug: str,
    session: Session = Depends(get_db),
):
    normalized = slug.strip().lower()
    event = session.query(NetworkEvent).filter(NetworkEvent.slug == normalized).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = None
    messages: list[PublicOrganizationChatMessageResponse] = []
    timeout = httpx.Timeout(connect=8.0, read=10.0, write=10.0, pool=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            ensured = await _matrix_ensure_event_chat_room(
                client=client,
                event_title=event.title,
                event_slug=event.slug,
                allow_create=ORG_MATRIX_AUTO_PROVISION_PUBLIC_EVENT_ROOMS,
            )
            room_id = str(ensured.get("room_id") or "").strip() or None
            room_alias = str(ensured.get("room_alias") or "").strip() or None
            room_name = str(ensured.get("room_name") or "").strip() or None
            if room_id:
                messages = await _matrix_recent_room_messages(client, room_id, limit=20)
    except Exception as exc:
        logger.warning("Unable to resolve event chat slug=%s error=%s", event.slug, exc)

    return PublicEventChatResponse(
        event_slug=event.slug,
        room_exists=bool(room_id),
        room_id=room_id,
        room_alias=room_alias,
        room_name=room_name,
        messages=messages,
    )


@router.post("/api/network/events", response_model=NetworkEventResponse)
async def create_network_event(
    payload: NetworkEventCreate,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    _throttle_action(f"network:create-event:{user_id}", limit=25, window_seconds=3600)

    if payload.ends_at and payload.starts_at and payload.ends_at < payload.starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be greater than or equal to starts_at")

    source_url = _validate_public_url(payload.source_url, "source_url")
    if source_url:
        existing = session.query(NetworkEvent).filter(NetworkEvent.source_url == source_url).first()
        if existing:
            raise HTTPException(status_code=409, detail="Event for this source URL already exists")

    resolved_host_type, resolved_host_user_id, resolved_host_org_id = _resolve_event_host_binding(
        host_type=payload.host_type,
        host_user_id=payload.host_user_id,
        host_org_id=payload.host_org_id,
        current_user=current_user,
        session=session,
    )
    slug = _ensure_unique_event_slug(session, payload.title)
    event = NetworkEvent(
        id=uuid.uuid4(),
        title=payload.title.strip(),
        slug=slug,
        description=payload.description,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        location=payload.location.strip() if payload.location else None,
        source_url=source_url,
        image_url=_validate_public_url(payload.image_url, "image_url"),
        tags=payload.tags or [],
        host_type=resolved_host_type,
        host_user_id=resolved_host_user_id,
        host_org_id=resolved_host_org_id,
        claimed_by_user_id=user_id if payload.claim_on_create else None,
        created_by_user_id=user_id,
        seeded_from_events=False,
    )
    session.add(event)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.created",
        target_type="network_event",
        target_id=str(event.id),
        metadata={
            "slug": event.slug,
            "title": event.title,
            "host_type": event.host_type,
            "claim_on_create": payload.claim_on_create,
        },
    )
    session.commit()
    session.refresh(event)
    if ORG_MATRIX_AUTO_PROVISION_PUBLIC_EVENT_ROOMS:
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await _matrix_ensure_event_chat_room(
                    client=client,
                    event_title=event.title,
                    event_slug=event.slug,
                    allow_create=True,
                )
        except Exception as exc:
            logger.warning("Matrix event room ensure skipped after event create slug=%s error=%s", event.slug, exc)
    return _map_network_event(event, current_user, session)


@router.post("/api/network/events/{event_id}/claim", response_model=NetworkEventResponse)
async def claim_network_event(
    event_id: uuid.UUID,
    payload: NetworkEventClaimRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:claim-event:{user_id}", limit=40, window_seconds=3600)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.claimed_by_user_id and event.claimed_by_user_id != user_id:
        raise HTTPException(status_code=409, detail="Event is already claimed")

    resolved_host_type, resolved_host_user_id, resolved_host_org_id = _resolve_event_host_binding(
        host_type=payload.host_type,
        host_user_id=payload.host_user_id,
        host_org_id=payload.host_org_id,
        current_user=current_user,
        session=session,
    )

    event.host_type = resolved_host_type
    event.host_user_id = resolved_host_user_id
    event.host_org_id = resolved_host_org_id
    event.claimed_by_user_id = user_id
    if not event.created_by_user_id:
        event.created_by_user_id = user_id
    event.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.claimed",
        target_type="network_event",
        target_id=str(event.id),
        metadata={
            "claimed_by": user_id,
            "host_type": event.host_type,
            "host_user_id": event.host_user_id,
            "host_org_id": str(event.host_org_id) if event.host_org_id else None,
        },
    )
    session.commit()
    session.refresh(event)
    return _map_network_event(event, current_user, session)


@router.post("/api/network/events/{event_id}/unclaim", response_model=NetworkEventResponse)
async def unclaim_network_event(
    event_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.claimed_by_user_id is None:
        return _map_network_event(event, current_user, session)
    if not _can_use_sysadmin_override(current_user, ["org:admin.write", "org:*"]) and event.claimed_by_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the claiming user or an admin can unclaim this event")

    previous_owner = event.claimed_by_user_id
    event.claimed_by_user_id = None
    event.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="event.unclaimed",
        target_type="network_event",
        target_id=str(event.id),
        metadata={"previous_owner": previous_owner},
    )
    session.commit()
    session.refresh(event)
    return _map_network_event(event, current_user, session)


@router.post("/api/network/events/{event_id}/attendance", response_model=EventAttendanceRecordResponse)
async def record_event_attendance(
    event_id: uuid.UUID,
    user_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    event = session.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    actor_user_id = _actor_user_id(current_user)
    target_user_id = (user_id or "").strip() or actor_user_id
    if target_user_id != actor_user_id and not _can_use_sysadmin_override(current_user, ["org:admin.write", "org:*"]):
        raise HTTPException(status_code=403, detail="Only SysAdmin can record attendance for another user")

    attendance = (
        session.query(EventAttendance)
        .filter(
            EventAttendance.event_id == event_id,
            EventAttendance.user_id == target_user_id,
        )
        .first()
    )
    if not attendance:
        attendance = EventAttendance(
            id=uuid.uuid4(),
            event_id=event_id,
            user_id=target_user_id,
        )
        session.add(attendance)

    if target_user_id == actor_user_id:
        attendance.user_email = current_user.get("email")
        attendance.user_name = current_user.get("name")
    attendance.attended_at = datetime.now(timezone.utc)
    attendance.source = "sysadmin_override" if target_user_id != actor_user_id else "self_checkin"
    attendance.verified_by_user_id = actor_user_id if target_user_id != actor_user_id else None
    session.commit()
    session.refresh(attendance)
    return attendance
