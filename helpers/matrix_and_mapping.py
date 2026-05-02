import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
from helpers.auth_and_scan import _actor_user_id, _is_sysadmin


def _org_source_urls(org: Organization) -> List[str]:
    urls = normalize_org_source_urls(list(org.source_urls or []))
    canonical = normalize_ingest_url(org.source_url)
    if canonical and canonical not in urls:
        urls.insert(0, canonical)
    return urls


def _matrix_localpart_for_pidp_user(pidp_user_id: str) -> str:
    base = re.sub(r"[^a-z0-9._=\-/]+", "-", (pidp_user_id or "").strip().lower()).strip("-")
    if not base:
        base = hashlib.sha256(pidp_user_id.encode("utf-8")).hexdigest()[:24]
    localpart = f"org-{base}"
    return localpart[:254]


def _matrix_user_id_for_pidp_user(pidp_user_id: str) -> str:
    localpart = _matrix_localpart_for_pidp_user(pidp_user_id)
    return f"@{localpart}:{ORG_MATRIX_SERVER_NAME}"


def _matrix_bootstrap_password(pidp_user_id: str) -> str:
    if not ORG_MATRIX_PASSWORD_SECRET:
        raise HTTPException(status_code=503, detail="Matrix bootstrap secret is not configured")
    digest = hmac.new(
        ORG_MATRIX_PASSWORD_SECRET.encode("utf-8"),
        pidp_user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"orgp-{digest}"


def _matrix_admin_headers() -> dict[str, str]:
    if not ORG_MATRIX_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Matrix bootstrap admin token is not configured")
    return {"Authorization": f"Bearer {ORG_MATRIX_ADMIN_TOKEN}"}


def _matrix_org_room_alias_candidates(org_slug: str) -> list[str]:
    normalized_slug = _slugify(org_slug)
    # Keep alias derivation deterministic and backward-compatible with likely variants.
    localparts = [
        f"org-{normalized_slug}-public",
        f"org-{normalized_slug}-public-chat",
        f"org-{normalized_slug}",
        normalized_slug,
        f"org-{normalized_slug}-chat",
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for localpart in localparts:
        alias = f"#{localpart}:{ORG_MATRIX_SERVER_NAME}"
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _matrix_org_room_alias_localpart(org_slug: str) -> str:
    normalized_slug = _slugify(org_slug)
    return f"org-{normalized_slug}-public"


async def _matrix_room_id_from_alias(
    client: httpx.AsyncClient,
    room_alias: str,
) -> Optional[str]:
    encoded_alias = quote(room_alias, safe="")
    response = await client.get(f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/directory/room/{encoded_alias}")
    if response.status_code == 404:
        return None
    if not response.is_success:
        return None
    payload = response.json() if response.content else {}
    room_id = str(payload.get("room_id") or "").strip()
    return room_id or None


async def _resolve_org_public_chat_room(
    client: httpx.AsyncClient,
    org_slug: str,
) -> tuple[Optional[str], Optional[str]]:
    for alias in _matrix_org_room_alias_candidates(org_slug):
        room_id = await _matrix_room_id_from_alias(client, alias)
        if room_id:
            return room_id, alias
    return None, None


def _matrix_retry_after_seconds(response: httpx.Response) -> float:
    retry_after_ms = 1000
    try:
        body = response.json() if response.content else {}
        retry_after_ms = int((body or {}).get("retry_after_ms") or retry_after_ms)
    except Exception:
        retry_after_ms = 1000
    return max(0.25, min(retry_after_ms / 1000.0, 60.0))


async def _matrix_create_room_with_retry(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    max_attempts: int = 4,
) -> httpx.Response:
    response: Optional[httpx.Response] = None
    for attempt in range(max_attempts):
        response = await client.post(
            f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/createRoom",
            headers=_matrix_admin_headers(),
            json=payload,
        )
        if response.status_code != 429:
            return response
        if attempt >= max_attempts - 1:
            return response
        await asyncio.sleep(_matrix_retry_after_seconds(response))
    if response is None:
        raise RuntimeError("Matrix create room request did not execute")
    return response


def _matrix_http_error_detail(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        errcode = str(payload.get("errcode") or "").strip().upper()
        retry_after_ms = int(payload.get("retry_after_ms") or 0) if payload.get("retry_after_ms") else 0
        if errcode == "M_LIMIT_EXCEEDED":
            if retry_after_ms > 0:
                wait_seconds = max(1, int((retry_after_ms + 999) // 1000))
                return f"Matrix is rate-limiting requests. Please retry in about {wait_seconds}s."
            return "Matrix is rate-limiting requests. Please retry shortly."
        message = str(payload.get("error") or payload.get("detail") or "").strip()
        if message:
            return message
    text = (response.text or "").strip()
    if text:
        return text[:300]
    return fallback


async def _matrix_create_public_org_room(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    alias_localpart = _matrix_org_room_alias_localpart(org_slug)
    primary_alias = f"#{alias_localpart}:{ORG_MATRIX_SERVER_NAME}"
    payload = {
        "visibility": "public",
        "preset": "public_chat",
        "name": (org_name or org_slug).strip()[:255] or org_slug,
        "topic": f"Public discussion space for {org_name or org_slug}",
        "room_alias_name": alias_localpart,
    }
    response = await _matrix_create_room_with_retry(client, payload)
    if response.is_success:
        body = response.json() if response.content else {}
        room_id = str(body.get("room_id") or "").strip()
        if room_id:
            logger.info("Provisioned Matrix public org room slug=%s room_id=%s", org_slug, room_id)
            return room_id, primary_alias, payload["name"]
        return None, None, None

    error_text = (response.text or "").strip().lower()
    # Race-safe behavior: if alias already exists, resolve it and continue.
    if response.status_code in {400, 409} and (
        "room alias" in error_text or "in use" in error_text or "m_room_in_use" in error_text
    ):
        existing_room_id = await _matrix_room_id_from_alias(client, primary_alias)
        if existing_room_id:
            logger.info("Reused existing Matrix public org room slug=%s room_id=%s", org_slug, existing_room_id)
            return existing_room_id, primary_alias, payload["name"]
    logger.warning(
        "Failed to provision Matrix public org room slug=%s status=%s body=%s",
        org_slug,
        response.status_code,
        (response.text or "").strip()[:400],
    )
    return None, None, None


async def _matrix_find_public_room_for_org(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    slug = _slugify(org_slug)
    name = (org_name or "").strip().lower()
    search_terms = [name, slug]
    for term in search_terms:
        if not term:
            continue
        response = await client.post(
            f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/publicRooms",
            json={
                "limit": 50,
                "filter": {"generic_search_term": term},
            },
        )
        if not response.is_success:
            continue
        payload = response.json() if response.content else {}
        chunk = payload.get("chunk") if isinstance(payload, dict) else None
        if not isinstance(chunk, list):
            continue
        for room in chunk:
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("room_id") or "").strip()
            canonical_alias = str(room.get("canonical_alias") or "").strip() or None
            room_name = str(room.get("name") or "").strip() or None
            if not room_id:
                continue
            alias_text = (canonical_alias or "").lower()
            room_name_text = (room_name or "").lower()
            if slug and (slug in alias_text or slug in room_name_text):
                return room_id, canonical_alias, room_name
            if name and name in room_name_text:
                return room_id, canonical_alias, room_name
    return None, None, None


def _matrix_org_general_alias_candidates(org_slug: str) -> list[str]:
    normalized_slug = _slugify(org_slug)
    localparts = [
        f"org-{normalized_slug}-public",
        f"org-{normalized_slug}-public-chat",
        f"org-{normalized_slug}",
        f"org-{normalized_slug}-general",
        f"org-{normalized_slug}-chat",
        f"{normalized_slug}-public",
        f"{normalized_slug}-public-chat",
        normalized_slug,
        f"{normalized_slug}-general",
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for localpart in localparts:
        alias = f"#{localpart}:{ORG_MATRIX_SERVER_NAME}"
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _matrix_org_announcements_alias_candidates(org_slug: str) -> list[str]:
    normalized_slug = _slugify(org_slug)
    localparts = [
        f"org-{normalized_slug}-announcements",
        f"org-{normalized_slug}-announcement",
        f"{normalized_slug}-announcements",
        f"{normalized_slug}-announcement",
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for localpart in localparts:
        alias = f"#{localpart}:{ORG_MATRIX_SERVER_NAME}"
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _matrix_org_announcements_alias_localpart(org_slug: str) -> str:
    normalized_slug = _slugify(org_slug)
    return f"org-{normalized_slug}-announcements"


def _matrix_event_room_alias_candidates(event_slug: str) -> list[str]:
    normalized_slug = _slugify(event_slug)
    localparts = [
        f"event-{normalized_slug}-chat",
        f"event-{normalized_slug}",
        f"{normalized_slug}-chat",
    ]
    aliases: list[str] = []
    seen: set[str] = set()
    for localpart in localparts:
        alias = f"#{localpart}:{ORG_MATRIX_SERVER_NAME}"
        if alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _matrix_event_room_alias_localpart(event_slug: str) -> str:
    normalized_slug = _slugify(event_slug)
    return f"event-{normalized_slug}-chat"


async def _matrix_create_public_event_room(
    client: httpx.AsyncClient,
    event_title: str,
    event_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    alias_localpart = _matrix_event_room_alias_localpart(event_slug)
    primary_alias = f"#{alias_localpart}:{ORG_MATRIX_SERVER_NAME}"
    display_name = (event_title or event_slug).strip()[:220] or event_slug
    payload = {
        "visibility": "public",
        "preset": "public_chat",
        "name": f"{display_name} • Event Chat",
        "topic": f"Public event discussion room for {display_name}",
        "room_alias_name": alias_localpart,
    }
    response = await _matrix_create_room_with_retry(client, payload)
    if response.is_success:
        body = response.json() if response.content else {}
        room_id = str(body.get("room_id") or "").strip()
        if room_id:
            logger.info("Provisioned Matrix event room slug=%s room_id=%s", event_slug, room_id)
            return room_id, primary_alias, payload["name"]
        return None, None, None

    error_text = (response.text or "").strip().lower()
    if response.status_code in {400, 409} and (
        "room alias" in error_text or "in use" in error_text or "m_room_in_use" in error_text
    ):
        existing_room_id = await _matrix_room_id_from_alias(client, primary_alias)
        if existing_room_id:
            logger.info("Reused existing Matrix event room slug=%s room_id=%s", event_slug, existing_room_id)
            return existing_room_id, primary_alias, payload["name"]
    logger.warning(
        "Failed to provision Matrix event room slug=%s status=%s body=%s",
        event_slug,
        response.status_code,
        (response.text or "").strip()[:400],
    )
    return None, None, None


async def _matrix_find_public_room_for_event(
    client: httpx.AsyncClient,
    event_title: str,
    event_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    slug = _slugify(event_slug)
    title = (event_title or "").strip().lower()
    search_terms = [slug, title]
    for term in search_terms:
        if not term:
            continue
        response = await client.post(
            f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/publicRooms",
            json={
                "limit": 70,
                "filter": {"generic_search_term": term},
            },
        )
        if not response.is_success:
            continue
        payload = response.json() if response.content else {}
        chunk = payload.get("chunk") if isinstance(payload, dict) else None
        if not isinstance(chunk, list):
            continue
        for room in chunk:
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("room_id") or "").strip()
            canonical_alias = str(room.get("canonical_alias") or "").strip() or None
            room_name = str(room.get("name") or "").strip() or None
            if not room_id:
                continue
            alias_text = (canonical_alias or "").lower()
            room_name_text = (room_name or "").lower()
            if slug and (slug in alias_text or slug in room_name_text):
                return room_id, canonical_alias, room_name
            if title and title in room_name_text:
                return room_id, canonical_alias, room_name
    return None, None, None


async def _matrix_ensure_event_chat_room(
    client: httpx.AsyncClient,
    event_title: str,
    event_slug: str,
    *,
    allow_create: bool,
) -> dict[str, Any]:
    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = f"{(event_title or event_slug).strip()[:220] or event_slug} • Event Chat"
    created = False

    for alias in _matrix_event_room_alias_candidates(event_slug):
        room_id = await _matrix_room_id_from_alias(client, alias)
        if room_id:
            room_alias = alias
            break
    if not room_id:
        discovered_room_id, discovered_alias, discovered_name = await _matrix_find_public_room_for_event(
            client=client,
            event_title=event_title,
            event_slug=event_slug,
        )
        if discovered_room_id:
            room_id = discovered_room_id
            room_alias = discovered_alias
            room_name = discovered_name or room_name
    if not room_id and allow_create:
        created_room_id, created_alias, created_name = await _matrix_create_public_event_room(
            client=client,
            event_title=event_title,
            event_slug=event_slug,
        )
        if created_room_id:
            room_id = created_room_id
            room_alias = created_alias
            room_name = created_name
            created = True
    return {
        "room_id": room_id,
        "room_alias": room_alias,
        "room_name": room_name,
        "created": created,
    }


async def _matrix_create_org_announcements_room(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    alias_localpart = _matrix_org_announcements_alias_localpart(org_slug)
    primary_alias = f"#{alias_localpart}:{ORG_MATRIX_SERVER_NAME}"
    display_name = (org_name or org_slug).strip()[:220] or org_slug
    payload = {
        "visibility": "public",
        "preset": "public_chat",
        "name": f"{display_name} Announcements",
        "topic": f"Official announcements for {display_name}",
        "room_alias_name": alias_localpart,
    }
    response = await _matrix_create_room_with_retry(client, payload)
    if response.is_success:
        body = response.json() if response.content else {}
        room_id = str(body.get("room_id") or "").strip()
        if room_id:
            logger.info("Provisioned Matrix announcements room slug=%s room_id=%s", org_slug, room_id)
            return room_id, primary_alias, payload["name"]
        return None, None, None

    error_text = (response.text or "").strip().lower()
    if response.status_code in {400, 409} and (
        "room alias" in error_text or "in use" in error_text or "m_room_in_use" in error_text
    ):
        existing_room_id = await _matrix_room_id_from_alias(client, primary_alias)
        if existing_room_id:
            logger.info("Reused existing Matrix announcements room slug=%s room_id=%s", org_slug, existing_room_id)
            return existing_room_id, primary_alias, payload["name"]
    logger.warning(
        "Failed to provision Matrix announcements room slug=%s status=%s body=%s",
        org_slug,
        response.status_code,
        (response.text or "").strip()[:400],
    )
    return None, None, None


async def _matrix_search_public_rooms_for_org(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
) -> list[dict[str, Any]]:
    search_terms = [(_slugify(org_slug) or "").strip(), (org_name or "").strip().lower()]
    rooms_by_id: dict[str, dict[str, Any]] = {}
    for term in search_terms:
        if not term:
            continue
        resp = await client.post(
            f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/publicRooms",
            json={"limit": 100, "filter": {"generic_search_term": term}},
        )
        if not resp.is_success:
            continue
        payload = resp.json() if resp.content else {}
        chunk = payload.get("chunk") if isinstance(payload, dict) else None
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if not isinstance(row, dict):
                continue
            room_id = str(row.get("room_id") or "").strip()
            if not room_id or room_id in rooms_by_id:
                continue
            rooms_by_id[room_id] = row
    return list(rooms_by_id.values())


def _matrix_pick_org_room_from_public_results(
    rooms: list[dict[str, Any]],
    org_slug: str,
    org_name: str,
    key: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    slug = _slugify(org_slug)
    name = (org_name or "").strip().lower()
    desired = "announc" if key == "announcements" else "public"
    best_match: tuple[int, str, Optional[str], Optional[str]] | None = None
    for row in rooms:
        room_id = str(row.get("room_id") or "").strip()
        if not room_id:
            continue
        alias = str(row.get("canonical_alias") or "").strip() or None
        room_name = str(row.get("name") or "").strip() or None
        topic = str(row.get("topic") or "").strip() or None
        haystack = " ".join(
            [
                (alias or "").lower(),
                (room_name or "").lower(),
                (topic or "").lower(),
            ]
        )
        if slug and slug not in haystack and not (name and name in haystack):
            continue
        score = 0
        if desired in haystack:
            score += 10
        if key in {"general", "public_chat"} and "chat" in haystack:
            score += 3
        if alias and alias.startswith(f"#org-{slug}"):
            score += 3
        if room_name and name and name in room_name.lower():
            score += 2
        if best_match is None or score > best_match[0]:
            best_match = (score, room_id, alias, room_name)
    if best_match is None:
        return None, None, None
    return best_match[1], best_match[2], best_match[3]


async def _matrix_join_room_as_admin(client: httpx.AsyncClient, room_ref: str) -> bool:
    encoded = quote(room_ref, safe="")
    resp = await client.post(
        f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/join/{encoded}",
        headers=_matrix_admin_headers(),
        json={},
    )
    if resp.is_success:
        return True
    if resp.status_code in {403, 404, 429}:
        return False
    # Already joined and some Synapse versions can return 400-ish states.
    body = (resp.text or "").lower()
    if "already in the room" in body:
        return True
    return False


async def _matrix_recent_room_messages(
    client: httpx.AsyncClient,
    room_id: str,
    limit: int = 20,
) -> list[PublicOrganizationChatMessageResponse]:
    encoded_room_id = quote(room_id, safe="")
    joined = await _matrix_join_room_as_admin(client, room_id)
    if not joined:
        return []
    resp = await client.get(
        f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/rooms/{encoded_room_id}/messages",
        headers=_matrix_admin_headers(),
        params={"dir": "b", "limit": max(1, min(limit, 50))},
    )
    if not resp.is_success:
        return []
    payload = resp.json() if resp.content else {}
    chunk = payload.get("chunk") if isinstance(payload, dict) else None
    if not isinstance(chunk, list):
        return []
    messages: list[PublicOrganizationChatMessageResponse] = []
    for event in chunk:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") != "m.room.message":
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        msgtype = str(content.get("msgtype") or "")
        if msgtype not in {"m.text", "m.notice"}:
            continue
        body = str(content.get("body") or "").strip()
        if not body:
            continue
        origin_ts = event.get("origin_server_ts")
        sent_at: Optional[datetime] = None
        try:
            if origin_ts is not None:
                sent_at = datetime.fromtimestamp(float(origin_ts) / 1000.0, tz=timezone.utc)
        except Exception:
            sent_at = None
        messages.append(
            PublicOrganizationChatMessageResponse(
                event_id=str(event.get("event_id") or ""),
                sender=str(event.get("sender") or "").strip() or None,
                body=body,
                sent_at=sent_at,
            )
        )
    messages.reverse()
    return messages


async def _matrix_ensure_org_chat_rooms(
    client: httpx.AsyncClient,
    org_name: str,
    org_slug: str,
    *,
    allow_create: bool,
) -> dict[str, dict[str, Any]]:
    public_room_id: Optional[str] = None
    public_alias: Optional[str] = None
    public_name: Optional[str] = org_name
    announcements_room_id: Optional[str] = None
    announcements_alias: Optional[str] = None
    announcements_name: Optional[str] = "Announcements"
    public_created = False
    announcements_created = False

    public_rooms = await _matrix_search_public_rooms_for_org(client, org_name, org_slug)

    for alias in _matrix_org_general_alias_candidates(org_slug):
        public_room_id = await _matrix_room_id_from_alias(client, alias)
        if public_room_id:
            public_alias = alias
            break
    if not public_room_id:
        public_room_id, public_alias, public_name = _matrix_pick_org_room_from_public_results(
            public_rooms,
            org_slug=org_slug,
            org_name=org_name,
            key="public_chat",
        )
    if not public_room_id and allow_create:
        created_id, created_alias, created_name = await _matrix_create_public_org_room(
            client=client,
            org_name=org_name,
            org_slug=org_slug,
        )
        if created_id:
            public_room_id = created_id
            public_alias = created_alias
            public_name = created_name
            public_created = True

    for alias in _matrix_org_announcements_alias_candidates(org_slug):
        announcements_room_id = await _matrix_room_id_from_alias(client, alias)
        if announcements_room_id:
            announcements_alias = alias
            break
    if not announcements_room_id:
        announcements_room_id, announcements_alias, announcements_name = _matrix_pick_org_room_from_public_results(
            public_rooms,
            org_slug=org_slug,
            org_name=org_name,
            key="announcements",
        )
    if not announcements_room_id and allow_create:
        created_id, created_alias, created_name = await _matrix_create_org_announcements_room(
            client=client,
            org_name=org_name,
            org_slug=org_slug,
        )
        if created_id:
            announcements_room_id = created_id
            announcements_alias = created_alias
            announcements_name = created_name
            announcements_created = True

    return {
        "public_chat": {
            "room_id": public_room_id,
            "room_alias": public_alias,
            "room_name": public_name or org_name,
            "created": public_created,
        },
        "announcements": {
            "room_id": announcements_room_id,
            "room_alias": announcements_alias,
            "room_name": announcements_name or "Announcements",
            "created": announcements_created,
        },
    }


async def _matrix_upsert_user(
    client: httpx.AsyncClient,
    matrix_user_id: str,
    password: str,
    display_name: str,
) -> None:
    encoded_user_id = quote(matrix_user_id, safe="")
    resp = await client.put(
        f"{ORG_MATRIX_HOMESERVER_URL}/_synapse/admin/v2/users/{encoded_user_id}",
        headers=_matrix_admin_headers(),
        json={
            "password": password,
            "displayname": (display_name or matrix_user_id)[:255],
            "admin": False,
            "deactivated": False,
            "logout_devices": False,
        },
    )
    if not resp.is_success:
        raise HTTPException(
            status_code=502,
            detail=_matrix_http_error_detail(resp, f"Matrix admin user upsert failed ({resp.status_code})"),
        )


async def _matrix_login_password(
    client: httpx.AsyncClient,
    matrix_user_id: str,
    password: str,
) -> MatrixBootstrapSessionResponse:
    login_resp = await client.post(
        f"{ORG_MATRIX_HOMESERVER_URL}/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {
                "type": "m.id.user",
                "user": matrix_user_id,
            },
            "password": password,
        },
    )
    if not login_resp.is_success:
        raise HTTPException(
            status_code=502,
            detail=_matrix_http_error_detail(login_resp, f"Matrix login failed ({login_resp.status_code})"),
        )
    payload = login_resp.json()
    access_token = str(payload.get("access_token") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not access_token or not user_id:
        raise HTTPException(status_code=502, detail="Matrix login response missing access_token or user_id")
    return MatrixBootstrapSessionResponse(
        access_token=access_token,
        user_id=user_id,
        device_id=payload.get("device_id"),
        homeserver_url=ORG_MATRIX_HOMESERVER_URL,
    )


async def _bootstrap_matrix_session_for_current_user(current_user: dict) -> MatrixBootstrapSessionResponse:
    _require_authenticated_user(current_user)
    pidp_user_id = _actor_user_id(current_user)
    if not pidp_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user id is required")
    matrix_user_id = _matrix_user_id_for_pidp_user(pidp_user_id)
    matrix_password = _matrix_bootstrap_password(pidp_user_id)
    display_name = str(current_user.get("name") or current_user.get("email") or matrix_user_id).strip()

    timeout = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Upsert guarantees the account exists and has the expected deterministic bootstrap password.
        await _matrix_upsert_user(
            client=client,
            matrix_user_id=matrix_user_id,
            password=matrix_password,
            display_name=display_name,
        )
        return await _matrix_login_password(
            client=client,
            matrix_user_id=matrix_user_id,
            password=matrix_password,
        )


def _slugify(value: str) -> str:
    return slugify(value)


def _ensure_unique_org_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(Organization).filter(Organization.slug == candidate).first()
        or any(
            isinstance(obj, Organization) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_event_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(NetworkEvent).filter(NetworkEvent.slug == candidate).first()
        or any(
            isinstance(obj, NetworkEvent) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_team_slug(session: Session, preferred: str) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while (
        session.query(Team).filter(Team.slug == candidate).first()
        or any(
            isinstance(obj, Team) and getattr(obj, "slug", None) == candidate
            for obj in session.new
        )
    ):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _ensure_unique_contact_slug(session: Session, preferred: str, excluding_user_id: Optional[str] = None) -> str:
    base = _slugify(preferred)
    candidate = base
    counter = 2
    while True:
        existing = session.query(UserContactPage).filter(UserContactPage.slug == candidate).first()
        if not existing or (excluding_user_id and existing.user_id == excluding_user_id):
            return candidate
        candidate = f"{base}-{counter}"
        counter += 1


def _map_org(org: Organization, current_user_id: Optional[str] = None) -> OrganizationResponse:
    my_role = None
    if current_user_id:
        for membership in org.memberships or []:
            if membership.user_id == current_user_id:
                my_role = membership.role
                break
    return OrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        claimed_by_user_id=org.claimed_by_user_id,
        created_by_user_id=org.created_by_user_id,
        membership_count=len(org.memberships or []),
        my_role=my_role,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _normalize_bot_tags(values: Optional[List[str]]) -> List[str]:
    if not values:
        return []
    deduped: List[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw or "").strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _map_network_bot(bot: NetworkBot) -> NetworkBotResponse:
    return NetworkBotResponse(
        id=bot.id,
        email=bot.email,
        full_name=bot.full_name,
        pidp_user_id=bot.pidp_user_id,
        description=bot.description,
        tags=list(bot.tags or []),
        active=bool(bot.active),
        created_by_user_id=bot.created_by_user_id,
        updated_by_user_id=bot.updated_by_user_id,
        last_token_issued_at=bot.last_token_issued_at,
        last_token_scope=bot.last_token_scope,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


def _map_public_org(
    org: Organization,
    session: Session,
    redirected_from_slug: Optional[str] = None,
) -> PublicOrganizationResponse:
    now_utc = datetime.now(timezone.utc)
    upcoming_events_count = (
        session.query(NetworkEvent)
        .filter(
            NetworkEvent.host_org_id == org.id,
            (
                (NetworkEvent.ends_at.isnot(None) & (NetworkEvent.ends_at >= now_utc))
                | (NetworkEvent.ends_at.is_(None) & NetworkEvent.starts_at.isnot(None) & (NetworkEvent.starts_at >= now_utc))
            ),
        )
        .count()
    )
    pending_claim_requests_count = (
        session.query(OrganizationClaimRequest)
        .filter(
            OrganizationClaimRequest.organization_id == org.id,
            OrganizationClaimRequest.status == "pending",
        )
        .count()
    )
    return PublicOrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        upcoming_events_count=upcoming_events_count,
        pending_claim_requests_count=pending_claim_requests_count,
        is_contested=bool(pending_claim_requests_count > 0),
        redirected_from_slug=redirected_from_slug,
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _map_public_org_list_item(
    org: Organization,
    membership_count: int,
    upcoming_events_count: int,
    pending_claim_requests_count: int,
) -> PublicOrganizationListItemResponse:
    return PublicOrganizationListItemResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        source_url=org.source_url,
        source_urls=_org_source_urls(org),
        image_url=org.image_url,
        tags=list(org.tags or []),
        seeded_from_events=bool(org.seeded_from_events),
        membership_count=int(membership_count or 0),
        upcoming_events_count=int(upcoming_events_count or 0),
        pending_claim_requests_count=int(pending_claim_requests_count or 0),
        is_contested=bool((pending_claim_requests_count or 0) > 0),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


def _map_governance_motion(motion: GovernanceMotion) -> GovernanceMotionResponse:
    return GovernanceMotionResponse(
        id=motion.id,
        type=motion.type,
        parent_motion_id=motion.parent_motion_id,
        title=motion.title,
        body=motion.body,
        proposed_body_diff=motion.proposed_body_diff,
        status=motion.status,
        proposer_type=motion.proposer_type,
        proposer_id=motion.proposer_user_id,
        proposer_name=motion.proposer_name,
        proposer_user_name=motion.proposer_user_name,
        proposer_org_id=motion.proposer_org_id,
        proposer_org_name=motion.proposer_org_name,
        seconder_id=motion.seconder_id,
        seconder_name=motion.seconder_name,
        discussion_deadline=motion.discussion_deadline,
        voting_deadline=motion.voting_deadline,
        quorum_required=motion.quorum_required,
        is_dissolution=(motion.type == GovernanceMotionType.DISSOLUTION.value),
        created_at=motion.created_at,
        updated_at=motion.updated_at,
    )


def _dissolution_required_yea(participating_voters: int) -> int:
    if participating_voters <= 0:
        return 0
    return (3 * participating_voters + 3) // 4


def _governance_vote_result(motion: GovernanceMotion) -> Dict[str, Any]:
    yea = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.YEA.value)
    nay = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.NAY.value)
    abstain = sum(1 for v in motion.votes if v.choice == GovernanceVoteChoice.ABSTAIN.value)
    participating_voters = yea + nay + abstain
    quorum_met = participating_voters >= int(motion.quorum_required or 0)
    threshold_rule = "simple_majority"
    required_yea = nay + 1
    passed = quorum_met and yea > nay
    if motion.type == GovernanceMotionType.DISSOLUTION.value:
        threshold_rule = "three_fourths_majority_of_participating_voters"
        required_yea = _dissolution_required_yea(participating_voters)
        passed = quorum_met and yea >= required_yea
    return {
        "yea": yea,
        "nay": nay,
        "abstain": abstain,
        "total_eligible": int(motion.quorum_required or 0),
        "participating_voters": participating_voters,
        "threshold_rule": threshold_rule,
        "required_yea": required_yea,
        "quorum_met": quorum_met,
        "passed": passed,
    }


def _governance_reaction_counts(motion: GovernanceMotion) -> GovernanceVoteCountsResponse:
    up = sum(1 for r in motion.reactions if r.direction == GovernanceReactionType.UP.value)
    down = sum(1 for r in motion.reactions if r.direction == GovernanceReactionType.DOWN.value)
    return GovernanceVoteCountsResponse(up=up, down=down, score=up - down)


def _get_dissolution_plan(
    session: Session,
    motion_id: uuid.UUID,
) -> Optional[GovernanceDissolutionPlan]:
    return (
        session.query(GovernanceDissolutionPlan)
        .filter(GovernanceDissolutionPlan.motion_id == motion_id)
        .first()
    )


def _validate_dissolution_payload(payload: GovernanceMotionCreate) -> None:
    if payload.type != GovernanceMotionType.DISSOLUTION.value:
        return
    if payload.parent_motion_id:
        raise HTTPException(status_code=422, detail="Dissolution motions cannot be amendments")
    asset_disposition = (payload.dissolution_asset_disposition or "").strip()
    recipient_name = (payload.dissolution_asset_recipient_name or "").strip()
    recipient_type = (payload.dissolution_asset_recipient_type or "").strip()
    if not asset_disposition:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_disposition is required for dissolution motions",
        )
    if not recipient_name:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_recipient_name is required for dissolution motions",
        )
    if recipient_type not in {"non_profit", "other_legal_entity"}:
        raise HTTPException(
            status_code=422,
            detail="dissolution_asset_recipient_type must be non_profit or other_legal_entity",
        )


def _can_manage_governance_motion(
    motion: GovernanceMotion,
    current_user: dict,
    session: Session,
) -> bool:
    if _is_sysadmin(current_user):
        return True
    user_id = _actor_user_id(current_user)
    if not user_id:
        return False
    if motion.proposer_user_id == user_id:
        return True
    if motion.proposer_type == GovernanceProposerType.ORG.value and motion.proposer_org_id:
        org = session.query(Organization).filter(Organization.id == motion.proposer_org_id).first()
        if org and _is_org_admin(org, current_user):
            return True
    return False


def _ensure_governance_transition(motion: GovernanceMotion, target_status: str) -> None:
    transitions = {
        GovernanceMotionStatus.PROPOSED.value: {
            GovernanceMotionStatus.SECONDED.value,
            GovernanceMotionStatus.WITHDRAWN.value,
            GovernanceMotionStatus.DISCUSSION.value,
        },
        GovernanceMotionStatus.SECONDED.value: {GovernanceMotionStatus.DISCUSSION.value},
        GovernanceMotionStatus.DISCUSSION.value: {
            GovernanceMotionStatus.VOTING.value,
            GovernanceMotionStatus.TABLED.value,
        },
        GovernanceMotionStatus.VOTING.value: {
            GovernanceMotionStatus.PASSED.value,
            GovernanceMotionStatus.FAILED.value,
        },
        GovernanceMotionStatus.TABLED.value: {GovernanceMotionStatus.DISCUSSION.value},
    }
    if not is_transition_allowed(motion.status, target_status, transitions):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status transition from {motion.status} to {target_status}",
        )


def _map_network_event(event: NetworkEvent, current_user: Optional[dict], session: Session) -> NetworkEventResponse:
    user_id = _actor_user_id(current_user or {})
    my_host_role = None

    if user_id:
        if event.claimed_by_user_id == user_id:
            my_host_role = "owner"
        elif event.host_type == EventHostType.INDIVIDUAL.value and event.host_user_id == user_id:
            my_host_role = "host_individual"
        elif event.host_type == EventHostType.ORG.value and event.host_org_id:
            org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
            if org and _is_org_admin(org, current_user or {}):
                my_host_role = "host_org_admin"

    host_org_name = None
    if event.host_type == EventHostType.ORG.value and event.host_org_id:
        host_org = session.query(Organization).filter(Organization.id == event.host_org_id).first()
        if host_org:
            host_org_name = host_org.name
    source_url = (event.source_url or "").strip().lower()
    represented_in_codecollective_source = bool(
        event.seeded_from_events
        or "codecollective.us" in source_url
    )

    return NetworkEventResponse(
        id=event.id,
        title=event.title,
        slug=event.slug,
        description=event.description,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        location=event.location,
        source_url=event.source_url,
        image_url=event.image_url,
        tags=list(event.tags or []),
        host_type=event.host_type,
        host_user_id=event.host_user_id,
        host_org_id=event.host_org_id,
        host_org_name=host_org_name,
        claimed_by_user_id=event.claimed_by_user_id,
        created_by_user_id=event.created_by_user_id,
        seeded_from_events=bool(event.seeded_from_events),
        represented_in_codecollective_source=represented_in_codecollective_source,
        is_unclaimed=event.claimed_by_user_id is None,
        my_host_role=my_host_role,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


def _resolve_event_host_binding(
    *,
    host_type: str,
    host_user_id: Optional[str],
    host_org_id: Optional[uuid.UUID],
    current_user: dict,
    session: Session,
) -> tuple[str, Optional[str], Optional[uuid.UUID]]:
    user_id = _actor_user_id(current_user)
    normalized_type = host_type.strip().lower()
    normalized_user_id = host_user_id.strip() if host_user_id else None

    if normalized_type == EventHostType.UNCLAIMED.value:
        if normalized_user_id or host_org_id:
            raise HTTPException(status_code=422, detail="Unclaimed host type cannot include host_user_id or host_org_id")
        return EventHostType.UNCLAIMED.value, None, None

    if normalized_type == EventHostType.INDIVIDUAL.value:
        if host_org_id:
            raise HTTPException(status_code=422, detail="Individual host type cannot include host_org_id")
        target_user_id = normalized_user_id or user_id
        if not target_user_id:
            raise HTTPException(status_code=401, detail="Authentication required for individual host")
        if target_user_id != user_id and not _is_sysadmin(current_user):
            raise HTTPException(status_code=403, detail="Cannot assign event to a different user")
        return EventHostType.INDIVIDUAL.value, target_user_id, None

    if normalized_type == EventHostType.ORG.value:
        if normalized_user_id:
            raise HTTPException(status_code=422, detail="Org host type cannot include host_user_id")
        if not host_org_id:
            raise HTTPException(status_code=422, detail="host_org_id is required when host_type='org'")
        org = session.query(Organization).filter(Organization.id == host_org_id).first()
        if not org:
            raise HTTPException(status_code=404, detail="Host organization not found")
        if not _is_org_admin(org, current_user):
            raise HTTPException(status_code=403, detail="Organization admin access required for org-hosted events")
        return EventHostType.ORG.value, None, host_org_id

    raise HTTPException(status_code=422, detail="Unsupported host_type")


def _extract_bearer_token(authorization: Optional[str]) -> str:
    return extract_bearer_token(authorization)
