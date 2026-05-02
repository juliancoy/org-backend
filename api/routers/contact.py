import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from fastapi import APIRouter

router = APIRouter(tags=["contact"])

@router.get("/api/network/contact/me", response_model=ContactPageResponse)
async def get_my_contact_page(
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)
        session.commit()
        session.refresh(contact)
    return _map_contact(contact, request)


@router.put("/api/network/contact/me", response_model=ContactPageResponse)
async def update_my_contact_page(
    payload: ContactPageUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:contact-update:{user_id}", limit=120, window_seconds=3600)
    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)

    if payload.slug is not None:
        contact.slug = _ensure_unique_contact_slug(session, payload.slug, excluding_user_id=user_id)
    if payload.enabled is not None:
        contact.enabled = payload.enabled
    if payload.headline is not None:
        contact.headline = payload.headline
    if payload.bio is not None:
        contact.bio = payload.bio
    if payload.photo_url is not None:
        contact.photo_url = _validate_public_url(payload.photo_url, "photo_url")
    if payload.email_public is not None:
        contact.email_public = payload.email_public
    if payload.phone_public is not None:
        contact.phone_public = payload.phone_public
    if payload.linkedin_url is not None:
        contact.linkedin_url = _validate_public_url(payload.linkedin_url, "linkedin_url")
    if payload.github_url is not None:
        contact.github_url = _validate_public_url(payload.github_url, "github_url")
    if payload.x_url is not None:
        contact.x_url = _validate_public_url(payload.x_url, "x_url")
    if payload.website_url is not None:
        contact.website_url = _validate_public_url(payload.website_url, "website_url")
    if payload.links is not None:
        normalized_links: list[dict[str, str]] = []
        for item in payload.links:
            normalized_links.append(
                {
                    "label": item.label.strip(),
                    "url": _validate_public_url(item.url, f"links[{item.label}]") or item.url,
                }
            )
        contact.links = normalized_links

    contact.user_email = current_user.get("email")
    contact.user_name = current_user.get("name")
    contact.updated_at = datetime.now(timezone.utc)
    _audit_event(
        session,
        actor=current_user,
        event_type="contact_page.updated",
        target_type="user_contact_page",
        target_id=user_id,
        metadata={"enabled": bool(contact.enabled), "slug": contact.slug},
    )
    session.commit()
    session.refresh(contact)
    return _map_contact(contact, request)


@router.post("/api/network/contact/me/import", response_model=ContactImportResponse)
async def import_my_contact_page(
    payload: ContactImportPayload,
    request: Request,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    user_id = _actor_user_id(current_user)
    _throttle_action(f"network:contact-import:{user_id}", limit=30, window_seconds=3600)

    safe_source_url = _ensure_public_fetch_url(payload.source_url, "source_url")
    imported = _fetch_public_profile_import(safe_source_url)

    contact = session.query(UserContactPage).filter(UserContactPage.user_id == user_id).first()
    if not contact:
        slug = _ensure_unique_contact_slug(session, _default_contact_slug(current_user))
        contact = UserContactPage(
            id=uuid.uuid4(),
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            slug=slug,
            enabled=False,
            links=[],
        )
        session.add(contact)

    changed_fields = _apply_contact_import_to_record(contact, imported, payload.overwrite)
    contact.source_profile_url = safe_source_url
    contact.source_profile_imported_at = datetime.now(timezone.utc)
    contact.user_email = current_user.get("email")
    contact.user_name = current_user.get("name")
    contact.updated_at = datetime.now(timezone.utc)

    _audit_event(
        session,
        actor=current_user,
        event_type="contact_page.imported",
        target_type="user_contact_page",
        target_id=user_id,
        metadata={
            "source_url": safe_source_url,
            "overwrite": bool(payload.overwrite),
            "changed_fields": changed_fields,
        },
    )
    session.commit()
    session.refresh(contact)
    return ContactImportResponse(
        contact=_map_contact(contact, request),
        imported_fields=changed_fields,
        source_url=safe_source_url,
    )


@router.get("/api/network/contact/{slug}", response_model=ContactPageResponse)
async def get_public_contact_page(
    slug: str,
    request: Request,
    session: Session = Depends(get_db),
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Contact page not found")
    return _map_contact(contact, request)


@router.get("/api/network/users/public", response_model=List[PublicUserListItemResponse])
async def list_public_user_profiles(
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 120,
    offset: int = 0,
    sort: str = Query("popular", pattern="^(popular|name|recent)$"),
):
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)

    upcoming_event_counts = (
        session.query(
            NetworkEvent.host_user_id.label("user_id"),
            func.count(NetworkEvent.id).label("upcoming_events_count"),
        )
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id.isnot(None),
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .group_by(NetworkEvent.host_user_id)
        .subquery()
    )

    upcoming_events_count_col = func.coalesce(upcoming_event_counts.c.upcoming_events_count, 0)
    query = (
        session.query(UserContactPage, upcoming_events_count_col)
        .outerjoin(upcoming_event_counts, upcoming_event_counts.c.user_id == UserContactPage.user_id)
        .filter(UserContactPage.enabled.is_(True))
    )

    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (UserContactPage.user_name.ilike(needle))
            | (UserContactPage.slug.ilike(needle))
            | (UserContactPage.headline.ilike(needle))
            | (UserContactPage.bio.ilike(needle))
        )

    if sort == "name":
        query = query.order_by(UserContactPage.user_name.asc(), UserContactPage.slug.asc())
    elif sort == "recent":
        query = query.order_by(UserContactPage.updated_at.desc(), UserContactPage.user_name.asc())
    else:
        query = query.order_by(
            upcoming_events_count_col.desc(),
            UserContactPage.updated_at.desc(),
            UserContactPage.user_name.asc(),
        )

    rows = query.offset(safe_offset).limit(safe_limit).all()
    return [
        _map_public_user_list_item(contact=contact, upcoming_events_count=upcoming_events_count)
        for contact, upcoming_events_count in rows
    ]


@router.get("/api/network/users/public/{slug}", response_model=PublicUserProfileResponse)
async def get_public_user_profile(
    slug: str,
    request: Request,
    session: Session = Depends(get_db),
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Public user profile not found")
    return _map_public_user_profile(contact, request, session)


@router.get("/api/network/users/public/{slug}/events", response_model=List[NetworkEventResponse])
async def list_public_user_events(
    slug: str,
    session: Session = Depends(get_db),
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
):
    contact = session.query(UserContactPage).filter(UserContactPage.slug == _slugify(slug)).first()
    if not contact or not contact.enabled:
        raise HTTPException(status_code=404, detail="Public user profile not found")

    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, min(offset, 100000))
    now_utc = datetime.now(timezone.utc)
    query = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_type == EventHostType.INDIVIDUAL.value,
            NetworkEvent.host_user_id == contact.user_id,
        )
        .order_by(NetworkEvent.starts_at.asc().nullslast(), NetworkEvent.created_at.desc())
    )
    if upcoming_only:
        query = query.filter(
            (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
            | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
        )

    events = query.offset(safe_offset).limit(safe_limit).all()
    return [_map_network_event(event, None, session) for event in events]


@router.get("/api/network/users", response_model=List[NetworkUserListItemResponse])
async def list_network_users(
    current_user: dict = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 300,
    offset: int = 0,
    sort: str = Query("recent", pattern="^(recent|name)$"),
):
    _require_authenticated_user(current_user)
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))

    query = session.query(Account).filter(Account.entity_type == EntityType.INDIVIDUAL)
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (Account.name.ilike(needle))
            | (Account.email.ilike(needle))
        )

    if sort == "name":
        query = query.order_by(Account.name.asc(), Account.created_at.desc())
    else:
        query = query.order_by(Account.created_at.desc(), Account.name.asc())

    users = query.offset(safe_offset).limit(safe_limit).all()
    user_ids = [str(user.id) for user in users]
    contacts = (
        session.query(UserContactPage)
        .filter(UserContactPage.user_id.in_(user_ids))
        .all()
        if user_ids
        else []
    )
    contact_by_user_id = {str(contact.user_id): contact for contact in contacts}
    pidp_avatar_by_email: dict[str, str] = {}
    missing_photo_emails = {
        str(user.email or "").strip().lower()
        for user in users
        if user.email and not (contact_by_user_id.get(str(user.id)) and contact_by_user_id.get(str(user.id)).photo_url)
    }
    if credentials and missing_photo_emails:
        pidp_avatar_by_email = await _fetch_pidp_avatar_map_by_email(credentials.credentials, missing_photo_emails)

    return [
        NetworkUserListItemResponse(
            user_id=str(user.id),
            user_name=user.name,
            email=user.email,
            created_at=user.created_at,
            contact_slug=(contact_by_user_id.get(str(user.id)).slug if contact_by_user_id.get(str(user.id)) else None),
            contact_enabled=bool(contact_by_user_id.get(str(user.id)).enabled) if contact_by_user_id.get(str(user.id)) else False,
            headline=contact_by_user_id.get(str(user.id)).headline if contact_by_user_id.get(str(user.id)) else None,
            photo_url=(
                contact_by_user_id.get(str(user.id)).photo_url
                if contact_by_user_id.get(str(user.id)) and contact_by_user_id.get(str(user.id)).photo_url
                else pidp_avatar_by_email.get(str(user.email or "").strip().lower())
            ),
        )
        for user in users
    ]


@router.get("/api/network/bots", response_model=List[NetworkBotResponse])
async def list_network_bots(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    q: str = "",
    limit: int = 250,
    offset: int = 0,
    active_only: bool = True,
):
    _require_authenticated_user(current_user)
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.read", "org:*"],
        detail="SysAdmin access required",
    )

    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(NetworkBot)
    if active_only:
        query = query.filter(NetworkBot.active.is_(True))
    if q.strip():
        needle = f"%{q.strip()}%"
        query = query.filter(
            (NetworkBot.email.ilike(needle))
            | (NetworkBot.full_name.ilike(needle))
            | (NetworkBot.description.ilike(needle))
        )

    rows = (
        query.order_by(NetworkBot.created_at.desc(), NetworkBot.email.asc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )
    return [_map_network_bot(row) for row in rows]


@router.post("/api/network/bots/provision", response_model=NetworkBotProvisionResponse)
async def provision_network_bot(
    payload: NetworkBotProvisionRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.write", "org:*"],
        detail="SysAdmin access required",
    )

    email = payload.email.strip().lower()
    if not re.fullmatch(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}$", email):
        raise HTTPException(status_code=422, detail="A valid bot email is required")

    full_name = (payload.full_name or "").strip() or "Portal Bot"
    timeout = httpx.Timeout(connect=12.0, read=12.0, write=12.0, pool=12.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        register_resp = await client.post(
            f"{PIDP_BASE_URL}/auth/register",
            json={
                "email": email,
                "password": payload.password,
                "full_name": full_name,
            },
        )
        if register_resp.status_code not in {200, 201, 409}:
            detail = register_resp.text.strip() or f"PIdP register failed ({register_resp.status_code})"
            raise HTTPException(status_code=register_resp.status_code, detail=detail)

        login_resp = await client.post(
            f"{PIDP_BASE_URL}/auth/token",
            data={
                "username": email,
                "password": payload.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not login_resp.is_success:
            detail = login_resp.text.strip() or "Unable to authenticate bot user in PIdP"
            raise HTTPException(status_code=400, detail=detail)
        login_payload = login_resp.json()
        bot_access_token = str(login_payload.get("access_token") or "").strip()
        if not bot_access_token:
            raise HTTPException(status_code=400, detail="Bot login succeeded without access token")

        bot_identity_resp = await client.get(
            f"{PIDP_BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {bot_access_token}"},
        )
        if not bot_identity_resp.is_success:
            detail = bot_identity_resp.text.strip() or "Unable to load bot identity from PIdP"
            raise HTTPException(status_code=400, detail=detail)
        bot_identity = bot_identity_resp.json()
        pidp_user_id = str(bot_identity.get("id") or "").strip()
        if not pidp_user_id:
            raise HTTPException(status_code=400, detail="PIdP bot identity did not include a user id")

        issued_api_token: Optional[str] = None
        issued_api_token_name: Optional[str] = None
        issued_api_token_scope: Optional[str] = None
        if payload.issue_api_token:
            token_issue_resp = await client.post(
                f"{PIDP_BASE_URL}/auth/tokens",
                headers={"Authorization": f"Bearer {bot_access_token}"},
                json={
                    "name": payload.api_token_name.strip(),
                    "scope": payload.api_token_scope.strip(),
                },
            )
            if not token_issue_resp.is_success:
                detail = token_issue_resp.text.strip() or "Unable to issue bot API token"
                raise HTTPException(status_code=400, detail=detail)
            token_issue_payload = token_issue_resp.json()
            issued_api_token = str(token_issue_payload.get("token") or "").strip() or None
            issued_api_token_name = str(token_issue_payload.get("name") or "").strip() or payload.api_token_name.strip()
            issued_api_token_scope = str(token_issue_payload.get("scope") or "").strip() or payload.api_token_scope.strip()

    actor_user_id = _actor_user_id(current_user)
    bot = (
        session.query(NetworkBot)
        .filter((NetworkBot.email == email) | (NetworkBot.pidp_user_id == pidp_user_id))
        .first()
    )
    if not bot:
        bot = NetworkBot(
            id=uuid.uuid4(),
            email=email,
            pidp_user_id=pidp_user_id,
            created_by_user_id=actor_user_id or None,
        )
        session.add(bot)

    bot.email = email
    bot.full_name = str(bot_identity.get("full_name") or full_name or email).strip()
    bot.pidp_user_id = pidp_user_id
    bot.description = payload.description
    bot.tags = _normalize_bot_tags(payload.tags)
    bot.active = True
    bot.updated_by_user_id = actor_user_id or None
    if issued_api_token_scope:
        bot.last_token_scope = issued_api_token_scope
        bot.last_token_issued_at = datetime.now(timezone.utc)

    _audit_event(
        session,
        actor=current_user,
        event_type="network.bot.provisioned",
        target_type="network_bot",
        target_id=str(bot.id),
        metadata={
            "email": bot.email,
            "pidp_user_id": bot.pidp_user_id,
            "issued_api_token": bool(issued_api_token),
            "issued_api_token_name": issued_api_token_name,
            "issued_api_token_scope": issued_api_token_scope,
        },
    )
    session.commit()
    session.refresh(bot)

    return NetworkBotProvisionResponse(
        bot=_map_network_bot(bot),
        issued_api_token=issued_api_token,
        issued_api_token_name=issued_api_token_name,
        issued_api_token_scope=issued_api_token_scope,
    )


@router.post("/api/network/bots/{bot_id}/issue-token", response_model=NetworkBotIssueTokenResponse)
async def issue_network_bot_token(
    bot_id: uuid.UUID,
    payload: NetworkBotIssueTokenRequest,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
