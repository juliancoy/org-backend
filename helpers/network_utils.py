import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value
from helpers.auth_and_scan import _actor_user_id, _is_sysadmin


def _require_ingest_auth(request: Request) -> None:
    expected = (os.getenv("ORG_INGEST_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Ingest token is not configured")
    provided = (
        request.headers.get("x-org-ingest-token")
        or _extract_bearer_token(request.headers.get("authorization"))
    )
    if not provided or not secrets.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


def _normalize_ingest_url(value: Optional[str]) -> Optional[str]:
    return normalize_ingest_url(value)


def _normalize_org_source_urls(values: Optional[List[str]]) -> List[str]:
    return normalize_org_source_urls(values)


def _org_source_urls(org: Organization) -> List[str]:
    urls = _normalize_org_source_urls(list(org.source_urls or []))
    canonical = _normalize_ingest_url(org.source_url)
    if canonical and canonical not in urls:
        urls.insert(0, canonical)
    return urls


def _set_org_source_urls(org: Organization, values: List[str]) -> None:
    normalized = _normalize_org_source_urls(values)
    org.source_urls = normalized
    if not org.source_url and normalized:
        org.source_url = normalized[0]


def _add_org_source_url(org: Organization, value: Optional[str]) -> None:
    url = _normalize_ingest_url(value)
    if not url:
        return
    merged = _org_source_urls(org)
    if url in merged:
        return
    merged.append(url)
    _set_org_source_urls(org, merged)


def _find_org_by_source_url(session: Session, value: Optional[str]) -> Optional[Organization]:
    url = _normalize_ingest_url(value)
    if not url:
        return None
    org = session.query(Organization).filter(Organization.source_url == url).first()
    if org:
        return org
    return session.query(Organization).filter(Organization.source_urls.contains([url])).first()


def _clean_ingest_tags(tags: Optional[List[str]], city: Optional[str] = None) -> List[str]:
    return clean_ingest_tags(tags, city)


def _derive_org_name(source_url: Optional[str], fallback: Optional[str] = None) -> str:
    return derive_org_name(source_url, fallback)


def _build_ingest_event_key(item: CalendarIngestEvent) -> str:
    if item.ingest_key and item.ingest_key.strip():
        return item.ingest_key.strip()
    material = "|".join(
        [
            str(item.city or "").strip().lower(),
            str(item.host_org_source_url or "").strip().lower(),
            str(item.source_url or "").strip().lower(),
            str(item.title or "").strip().lower(),
            item.starts_at.isoformat() if item.starts_at else "",
            item.ends_at.isoformat() if item.ends_at else "",
            str(item.location or "").strip().lower(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _coerce_calendar_datetime(value: Optional[Any]) -> Optional[datetime]:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _render_public_event_location(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        location = value.strip()
        return location or None
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    for key in ("name", "address", "city", "state", "postalCode", "country"):
        field_value = str(value.get(key) or "").strip()
        if field_value:
            parts.append(field_value)
    if not parts and value.get("latitude") is not None and value.get("longitude") is not None:
        parts.append(f"{value.get('latitude')}, {value.get('longitude')}")
    return ", ".join(parts) if parts else None


def _derive_public_event_org_name(raw_event: Dict[str, Any], host_org_source_url: Optional[str]) -> str:
    for key in ("org_name", "orgName", "source_group", "group_name"):
        candidate = str(raw_event.get(key) or "").strip()
        if candidate:
            return candidate
    organizer = raw_event.get("organizer")
    if isinstance(organizer, dict):
        name = str(organizer.get("name") or "").strip()
        if name:
            return name
    if isinstance(organizer, str):
        name = organizer.strip()
        if name:
            return name
    if host_org_source_url:
        return _derive_org_name(host_org_source_url, None)
    return "Organization"


def _city_from_feed_url(feed_url: str) -> Optional[str]:
    return city_from_feed_url(feed_url)


def _build_ingest_payload_from_public_feed(
    feed_url: str,
    raw_events: List[Dict[str, Any]],
) -> CalendarIngestPayload:
    city = _city_from_feed_url(feed_url)
    events: list[CalendarIngestEvent] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        title = str(raw_event.get("name") or raw_event.get("title") or "").strip()
        if not title:
            continue
        source_url = _normalize_ingest_url(raw_event.get("url") or raw_event.get("source_url"))
        host_org_source_url = _normalize_ingest_url(raw_event.get("source") or raw_event.get("source_url"))
        host_org_image_url = _normalize_ingest_url(raw_event.get("orgImageUrl") or raw_event.get("org_image_url"))
        event_image_url = _normalize_ingest_url(raw_event.get("imageUrl") or raw_event.get("image_url")) or host_org_image_url
        host_org_name = _derive_public_event_org_name(raw_event, host_org_source_url)
        events.append(
            CalendarIngestEvent(
                title=title,
                description=str(raw_event.get("description") or "").strip() or None,
                starts_at=_coerce_calendar_datetime(raw_event.get("startDate") or raw_event.get("starts_at")),
                ends_at=_coerce_calendar_datetime(raw_event.get("endDate") or raw_event.get("ends_at")),
                location=_render_public_event_location(raw_event.get("location")),
                source_url=source_url,
                host_org_source_url=host_org_source_url,
                host_org_name=host_org_name,
                host_org_image_url=host_org_image_url,
                image_url=event_image_url,
                tags=raw_event.get("tags") if isinstance(raw_event.get("tags"), list) else None,
                city=str(raw_event.get("city") or city or "").strip() or None,
            )
        )
    return CalendarIngestPayload(
        source="codecollective-public-json",
        generated_at=datetime.now(timezone.utc),
        organizations=[],
        events=events,
    )


def _upsert_ingested_organization(
    session: Session,
    item: CalendarIngestOrganization,
) -> tuple[Organization, bool]:
    source_url = _normalize_ingest_url(item.source_url)
    name = _derive_org_name(source_url, item.name)
    tags = _clean_ingest_tags(item.tags, item.city)
    image_url = _normalize_ingest_url(item.image_url)
    description = (item.description or "").strip() or None

    org = _find_org_by_source_url(session, source_url)
    if not org:
        candidate_slug = _slugify(name)
        org = session.query(Organization).filter(Organization.slug == candidate_slug).first()

    created = False
    if not org:
        org = Organization(
            id=uuid.uuid4(),
            name=name,
            slug=_ensure_unique_org_slug(session, name),
            description=description,
            source_url=source_url,
            source_urls=[source_url] if source_url else [],
            image_url=image_url,
            tags=tags,
            seeded_from_events=True,
        )
        session.add(org)
        created = True
    else:
        if not org.claimed_by_user_id:
            org.name = name
            if description:
                org.description = description
        _add_org_source_url(org, source_url)
        if image_url:
            org.image_url = image_url
        if tags:
            merged_tags = sorted(set((org.tags or []) + tags))
            org.tags = merged_tags
        org.seeded_from_events = True
        org.updated_at = datetime.now(timezone.utc)
    return org, created


def _upsert_ingested_event(
    session: Session,
    item: CalendarIngestEvent,
    host_org_by_source: Dict[str, Organization],
) -> tuple[Optional[NetworkEvent], str]:
    if item.ends_at and item.starts_at and item.ends_at < item.starts_at:
        return None, "skipped"

    ingest_key = _build_ingest_event_key(item)
    source_url = _normalize_ingest_url(item.source_url)
    host_org_source_url = _normalize_ingest_url(item.host_org_source_url)
    image_url = _normalize_ingest_url(item.image_url)
    tags = _clean_ingest_tags(item.tags, item.city)

    host_org = host_org_by_source.get(host_org_source_url or "")
    title = item.title.strip()
    if not title:
        return None, "skipped"

    event = session.query(NetworkEvent).filter(NetworkEvent.ingest_key == ingest_key).first()
    if not event and source_url and item.starts_at:
        event = (
            session.query(NetworkEvent)
            .filter(
                NetworkEvent.source_url == source_url,
                NetworkEvent.title == title,
                NetworkEvent.starts_at == item.starts_at,
            )
            .first()
        )

    # source_url is globally unique. Some feeds reuse one URL across many events.
    # Keep the first binding and drop conflicting source_url values for new rows.
    resolved_source_url = source_url
    if not event and resolved_source_url:
        existing_source = session.query(NetworkEvent).filter(NetworkEvent.source_url == resolved_source_url).first()
        if existing_source:
            same_instance = (
                existing_source.title == title
                or (item.starts_at and existing_source.starts_at == item.starts_at)
            )
            if same_instance:
                event = existing_source
            else:
                resolved_source_url = None
        elif any(
            isinstance(obj, NetworkEvent) and getattr(obj, "source_url", None) == resolved_source_url
            for obj in session.new
        ):
            resolved_source_url = None

    created = False
    if not event:
        event = NetworkEvent(
            id=uuid.uuid4(),
            title=title,
            slug=_ensure_unique_event_slug(session, f"{title}-{item.starts_at.date()}" if item.starts_at else title),
            description=(item.description or "").strip() or None,
            starts_at=item.starts_at,
            ends_at=item.ends_at,
            location=(item.location or "").strip() or None,
            source_url=resolved_source_url,
            ingest_key=ingest_key,
            image_url=image_url,
            tags=tags,
            host_type=EventHostType.ORG.value if host_org else EventHostType.UNCLAIMED.value,
            host_org_id=host_org.id if host_org else None,
            host_user_id=None,
            claimed_by_user_id=None,
            seeded_from_events=True,
        )
        session.add(event)
        created = True
    else:
        event.ingest_key = event.ingest_key or ingest_key
        event.title = title
        event.description = (item.description or "").strip() or None
        event.starts_at = item.starts_at
        event.ends_at = item.ends_at
        event.location = (item.location or "").strip() or None
        if resolved_source_url and resolved_source_url != event.source_url:
            existing_source_owner = (
                session.query(NetworkEvent)
                .filter(NetworkEvent.source_url == resolved_source_url, NetworkEvent.id != event.id)
                .first()
            )
            if not existing_source_owner:
                event.source_url = resolved_source_url
        if image_url:
            event.image_url = image_url
        if tags:
            event.tags = sorted(set((event.tags or []) + tags))
        if host_org and not event.claimed_by_user_id:
            event.host_type = EventHostType.ORG.value
            event.host_org_id = host_org.id
            event.host_user_id = None
        event.seeded_from_events = True
        event.updated_at = datetime.now(timezone.utc)

    return event, "created" if created else "updated"


def _validate_public_url(url: Optional[str], field_name: str) -> Optional[str]:
    if not url:
        return None
    cleaned = url.strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except Exception:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid URL")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise HTTPException(status_code=422, detail=f"{field_name} must start with http:// or https://")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise HTTPException(status_code=422, detail=f"{field_name} must include a hostname")
    if hostname in {"localhost"} or hostname.endswith(".local"):
        raise HTTPException(status_code=422, detail=f"{field_name} must use a public hostname")
    try:
        ip_value = ipaddress.ip_address(hostname)
    except ValueError:
        ip_value = None
    if ip_value and (
        ip_value.is_private
        or ip_value.is_loopback
        or ip_value.is_link_local
        or ip_value.is_multicast
        or ip_value.is_reserved
        or ip_value.is_unspecified
    ):
        raise HTTPException(status_code=422, detail=f"{field_name} must use a public hostname")
    return parsed._replace(fragment="").geturl()


def _is_disallowed_public_ip(ip_value: ipaddress._BaseAddress) -> bool:
    return (
        ip_value.is_private
        or ip_value.is_loopback
        or ip_value.is_link_local
        or ip_value.is_multicast
        or ip_value.is_reserved
        or ip_value.is_unspecified
    )


def _ensure_public_fetch_url(url: str, field_name: str = "source_url") -> str:
    normalized = _validate_public_url(url, field_name)
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid public URL")
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise HTTPException(status_code=422, detail=f"{field_name} must include a hostname")

    try:
        resolved = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=422, detail=f"{field_name} host could not be resolved")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} host validation failed: {exc}")

    for result in resolved:
        candidate = result[4][0]
        try:
            ip_value = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if _is_disallowed_public_ip(ip_value):
            raise HTTPException(status_code=422, detail=f"{field_name} must resolve to a public host")
    return normalized


def _truncate_preview_text(value: Optional[str], limit: int) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", " ", raw).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _meta_content_from_soup(soup: BeautifulSoup, *, property_name: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
    selector: dict[str, str] = {}
    if property_name:
        selector["property"] = property_name
    if name:
        selector["name"] = name
    if not selector:
        return None
    tag = soup.find("meta", attrs=selector)
    if not tag:
        return None
    return _truncate_preview_text(str(tag.get("content") or ""), 5000)


async def _fetch_chat_link_preview(url: str) -> ChatLinkPreviewResponse:
    safe_url = _ensure_public_fetch_url(url, "url")
    parsed_safe = urlparse(safe_url)
    if parsed_safe.username or parsed_safe.password:
        raise HTTPException(status_code=422, detail="url must not include embedded credentials")
    if parsed_safe.port and parsed_safe.port not in {80, 443}:
        raise HTTPException(status_code=422, detail="url must use standard http/https ports")

    timeout = httpx.Timeout(connect=6.0, read=6.0, write=6.0, pool=6.0)
    headers = {
        "User-Agent": "ArkavoOrgPortalLinkPreview/1.0 (+https://dev.portal.arkavo.org)",
        "Accept": "text/html,application/xhtml+xml;q=0.9",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, max_redirects=4) as client:
        response = await client.get(safe_url, headers=headers)

    if not response.is_success:
        raise HTTPException(status_code=422, detail=f"Link returned HTTP {response.status_code}")
    if len(response.content or b"") > 1_000_000:
        raise HTTPException(status_code=422, detail="Link preview source is too large")
    content_type = str(response.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise HTTPException(status_code=422, detail="Link preview supports HTML pages only")

    resolved_url = _ensure_public_fetch_url(str(response.url), "url")
    soup = BeautifulSoup(response.text or "", "html.parser")
    title = (
        _meta_content_from_soup(soup, property_name="og:title")
        or _meta_content_from_soup(soup, name="twitter:title")
        or _truncate_preview_text(soup.title.get_text(" ", strip=True) if soup.title else "", 180)
    )
    description = (
        _meta_content_from_soup(soup, property_name="og:description")
        or _meta_content_from_soup(soup, name="description")
        or _meta_content_from_soup(soup, name="twitter:description")
    )
    if description:
        description = _truncate_preview_text(description, 320)

    image_raw = (
        _meta_content_from_soup(soup, property_name="og:image")
        or _meta_content_from_soup(soup, name="twitter:image")
    )
    image_url = _coerce_public_url_candidate(urljoin(resolved_url, image_raw), "image_url") if image_raw else None

    canonical_raw = ""
    canonical_link = soup.find("link", attrs={"rel": lambda value: value and "canonical" in str(value).lower()})
    if canonical_link and canonical_link.get("href"):
        canonical_raw = str(canonical_link.get("href") or "").strip()
    canonical_url = _coerce_public_url_candidate(urljoin(resolved_url, canonical_raw), "canonical_url") if canonical_raw else None
    canonical_url = canonical_url or resolved_url

    parsed_domain = urlparse(canonical_url or resolved_url)
    domain = (parsed_domain.hostname or "").strip().lower() or None
    site_name = (
        _meta_content_from_soup(soup, property_name="og:site_name")
        or _meta_content_from_soup(soup, name="application-name")
        or domain
    )
    return ChatLinkPreviewResponse(
        url=safe_url,
        canonical_url=canonical_url,
        title=_truncate_preview_text(title, 180),
        description=description,
        image_url=image_url,
        site_name=_truncate_preview_text(site_name, 80),
        domain=domain,
    )


def _dedupe_contact_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for entry in links:
        label = str(entry.get("label") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not label or not url:
            continue
        key = (label.lower(), url.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"label": label, "url": url})
    return deduped


def _extract_contact_import_from_html(source_url: str, html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    title_text = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    h1_text = ""
    first_h1 = soup.find("h1")
    if first_h1:
        h1_text = first_h1.get_text(" ", strip=True).strip()

    headline = ""
    if title_text:
        headline = title_text.split("|")[0].strip()
    if not headline and h1_text:
        headline = h1_text

    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: list[dict[str, str]] = []

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        label = anchor.get_text(" ", strip=True) or "Link"
        href_lower = href.lower()

        if href_lower.startswith("mailto:"):
            candidate = href.split(":", 1)[1].strip()
            if candidate and not email_public:
                email_public = candidate.lower()
            continue
        if href_lower.startswith("tel:"):
            candidate = href.split(":", 1)[1].strip()
            if candidate and not phone_public:
                phone_public = candidate
            continue
        if not href_lower.startswith("http://") and not href_lower.startswith("https://"):
            continue

        normalized = _validate_public_url(href, "imported_link_url")
        if not normalized:
            continue
        host = (urlparse(normalized).hostname or "").lower()
        if "linkedin.com" in host and not linkedin_url:
            linkedin_url = normalized
        elif "github.com" in host and not github_url:
            github_url = normalized
        elif ("twitter.com" in host or "x.com" in host) and not x_url:
            x_url = normalized
        elif not website_url and host not in {"www.linkedin.com", "linkedin.com", "github.com", "www.github.com", "twitter.com", "www.twitter.com", "x.com", "www.x.com"}:
            website_url = normalized
        links.append({"label": label.strip()[:120] or "Link", "url": normalized})

    body = soup.body
    body_text = body.get_text("\n", strip=True) if body else soup.get_text("\n", strip=True)
    bio = ""
    if body_text:
        pre_contact = body_text.split("Contact Information", 1)[0].strip()
        if h1_text:
            pre_contact = re.sub(rf"^\s*{re.escape(h1_text)}\s*", "", pre_contact, count=1, flags=re.IGNORECASE).strip()
        pre_contact = re.sub(r"\s+", " ", pre_contact).strip()
        if pre_contact:
            bio = pre_contact[:2000]

    photo_url: Optional[str] = None
    main_section = soup.find(id="main") or soup.body
    if main_section:
        img = main_section.find("img")
        if img and img.get("src"):
            photo_url = _validate_public_url(urljoin(source_url, img.get("src")), "photo_url")

    return {
        "headline": headline[:255] if headline else None,
        "bio": bio or None,
        "photo_url": photo_url,
        "email_public": email_public,
        "phone_public": phone_public,
        "linkedin_url": linkedin_url,
        "github_url": github_url,
        "x_url": x_url,
        "website_url": website_url,
        "links": _dedupe_contact_links(links)[:20],
    }


def _fetch_public_profile_import(source_url: str) -> dict[str, Any]:
    try:
        response = requests.get(
            source_url,
            timeout=12,
            headers={
                "User-Agent": "ArkavoOrgPortalProfileImporter/1.0 (+https://dev.portal.arkavo.org)",
                "Accept": "text/html,application/xhtml+xml",
            },
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=422, detail=f"Failed to fetch source profile: {exc}")
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail=f"Source profile returned HTTP {response.status_code}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise HTTPException(status_code=422, detail="Source profile must be an HTML page")
    if len(response.content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Source profile is too large")
    final_url = _ensure_public_fetch_url(response.url, "source_url")
    return _extract_contact_import_from_html(final_url, response.text)


def _apply_contact_import_to_record(
    contact: UserContactPage,
    imported: dict[str, Any],
    overwrite: bool,
) -> list[str]:
    changed: list[str] = []

    url_fields = {"photo_url", "linkedin_url", "github_url", "x_url", "website_url"}
    simple_fields = {
        "headline",
        "bio",
        "photo_url",
        "email_public",
        "phone_public",
        "linkedin_url",
        "github_url",
        "x_url",
        "website_url",
    }

    for field_name in simple_fields:
        candidate = imported.get(field_name)
        if candidate is None:
            continue
        current_value = getattr(contact, field_name, None)
        if not overwrite and current_value:
            continue
        if field_name in url_fields:
            candidate = _validate_public_url(candidate, field_name)
        if current_value != candidate:
            setattr(contact, field_name, candidate)
            changed.append(field_name)

    imported_links = imported.get("links") or []
    if imported_links:
        normalized_links = _dedupe_contact_links(
            [
                {"label": str(item.get("label") or "").strip(), "url": _validate_public_url(item.get("url"), "links.url") or ""}
                for item in imported_links
                if isinstance(item, dict) and item.get("url")
            ]
        )
        if overwrite:
            if (contact.links or []) != normalized_links:
                contact.links = normalized_links
                changed.append("links")
        else:
            merged = _dedupe_contact_links(list(contact.links or []) + normalized_links)
            if merged != (contact.links or []):
                contact.links = merged
                changed.append("links")

    return changed


def _throttle_action(key: str, limit: int, window_seconds: int) -> None:
    try:
        redis_client = db.redis_client
        if redis_client is None:
            return
        value = redis_client.incr(key)
        if value == 1:
            redis_client.expire(key, window_seconds)
        if int(value) > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this action")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Rate limit check skipped: {exc}")


def _request_client_ip(request: Request) -> str:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    candidate = ""
    if forwarded_for:
        candidate = forwarded_for.split(",")[0].strip()
    if not candidate:
        candidate = (request.headers.get("x-real-ip") or "").strip()
    if not candidate and request.client:
        candidate = (request.client.host or "").strip()
    if not candidate:
        return "unknown"
    try:
        ip_obj = ipaddress.ip_address(candidate)
        return ip_obj.compressed
    except ValueError:
        return "unknown"


def _business_card_extension_for_content_type(content_type: str) -> str:
    normalized = (content_type or "").strip().lower()
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/png":
        return ".png"
    if normalized == "image/webp":
        return ".webp"
    return ".img"


def _business_card_storage_root() -> Path:
    return Path(ORG_BUSINESS_CARD_STORAGE_DIR).resolve()


def _business_card_storage_backend() -> str:
    backend = (ORG_BUSINESS_CARD_STORAGE_BACKEND or "local").strip().lower()
    if backend not in {"local", "s3"}:
        return "local"
    return backend


def _business_card_s3_object_key(
    *,
    submission_id: uuid.UUID,
    content_type: str,
    now: Optional[datetime] = None,
) -> str:
    ts = now or datetime.now(timezone.utc)
    extension = _business_card_extension_for_content_type(content_type)
    key_parts = [str(ts.year), f"{ts.month:02d}", f"{submission_id}{extension}"]
    if ORG_BUSINESS_CARD_S3_PREFIX:
        key_parts.insert(0, ORG_BUSINESS_CARD_S3_PREFIX)
    return "/".join(key_parts)


def _build_business_card_s3_client():
    if not ORG_BUSINESS_CARD_S3_ACCESS_KEY or not ORG_BUSINESS_CARD_S3_SECRET_KEY:
        raise RuntimeError("S3 storage credentials are not configured")
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:
        raise RuntimeError("boto3 is required for ORG_BUSINESS_CARD_STORAGE_BACKEND=s3") from exc

    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=ORG_BUSINESS_CARD_S3_ENDPOINT_URL or None,
        aws_access_key_id=ORG_BUSINESS_CARD_S3_ACCESS_KEY,
        aws_secret_access_key=ORG_BUSINESS_CARD_S3_SECRET_KEY,
        region_name=ORG_BUSINESS_CARD_S3_REGION,
        use_ssl=ORG_BUSINESS_CARD_S3_USE_SSL,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _ensure_business_card_s3_bucket() -> None:
    if not ORG_BUSINESS_CARD_STORAGE_ENABLED or _business_card_storage_backend() != "s3":
        return
    client = _build_business_card_s3_client()
    bucket = ORG_BUSINESS_CARD_S3_BUCKET
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception:
        pass
    create_kwargs = {"Bucket": bucket}
    if ORG_BUSINESS_CARD_S3_REGION and ORG_BUSINESS_CARD_S3_REGION != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": ORG_BUSINESS_CARD_S3_REGION
        }
    client.create_bucket(**create_kwargs)


def _persist_business_card_image(
    *,
    submission_id: uuid.UUID,
    image_bytes: bytes,
    content_type: str,
) -> tuple[str, Optional[str], str]:
    if not ORG_BUSINESS_CARD_STORAGE_ENABLED:
        return ("disabled", None, "")
    backend = _business_card_storage_backend()
    if backend == "s3":
        object_key = _business_card_s3_object_key(
            submission_id=submission_id,
            content_type=content_type,
        )
        client = _build_business_card_s3_client()
        client.put_object(
            Bucket=ORG_BUSINESS_CARD_S3_BUCKET,
            Key=object_key,
            Body=image_bytes,
            ContentType=content_type or "application/octet-stream",
            **(
                {"ServerSideEncryption": ORG_BUSINESS_CARD_S3_SERVER_SIDE_ENCRYPTION}
                if ORG_BUSINESS_CARD_S3_SERVER_SIDE_ENCRYPTION
                else {}
            ),
        )
        return ("s3", ORG_BUSINESS_CARD_S3_BUCKET, object_key)

    root = _business_card_storage_root()
    now = datetime.now(timezone.utc)
    relative_dir = Path(str(now.year), f"{now.month:02d}")
    target_dir = root / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    extension = _business_card_extension_for_content_type(content_type)
    target_name = f"{submission_id}{extension}"
    target_path = target_dir / target_name
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    temp_path.write_bytes(image_bytes)
    temp_path.replace(target_path)
    return ("local", None, str(relative_dir / target_name))


def _resolve_business_card_storage_path(relative_path: str) -> Path:
    root = _business_card_storage_root()
    candidate = (root / relative_path).resolve()
    if not str(candidate).startswith(str(root)):
        raise HTTPException(status_code=400, detail="Invalid storage path")
    return candidate


def _load_business_card_image_bytes(
    *,
    storage_backend: str,
    storage_bucket: Optional[str],
    storage_path: str,
) -> bytes:
    backend = (storage_backend or "local").strip().lower()
    if backend == "s3":
        bucket = (storage_bucket or ORG_BUSINESS_CARD_S3_BUCKET).strip()
        if not bucket:
            raise HTTPException(status_code=500, detail="S3 bucket not configured for stored image")
        client = _build_business_card_s3_client()
        response = client.get_object(Bucket=bucket, Key=storage_path)
        body = response.get("Body")
        data = body.read() if body else b""
        if not data:
            raise HTTPException(status_code=404, detail="Stored image file missing")
        return data

    image_path = _resolve_business_card_storage_path(storage_path)
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Stored image file missing")
    return image_path.read_bytes()


def _scan_created_target_image_url(
    submission_id: uuid.UUID,
    *,
    target_type: str,
    target_id: Optional[str],
) -> Optional[str]:
    normalized_type = str(target_type or "").strip().lower()
    normalized_id = str(target_id or "").strip()
    if normalized_type not in {"organization", "event"} or not normalized_id:
        return None
    return f"/api/network/scans/{submission_id}/image/public/{normalized_type}/{normalized_id}"


def _enforce_business_card_duplicate_hash_guard(
    session: Session,
    *,
    image_sha256: str,
    duplicate_hash_limit: int,
    duplicate_hash_window_seconds: int,
) -> None:
    if duplicate_hash_limit <= 0 or duplicate_hash_window_seconds <= 0:
        return
    window_start = datetime.now(timezone.utc) - timedelta(seconds=duplicate_hash_window_seconds)
    existing_count = (
        session.query(func.count(BusinessCardSubmission.id))
        .filter(
            BusinessCardSubmission.image_sha256 == image_sha256,
            BusinessCardSubmission.created_at >= window_start,
        )
        .scalar()
        or 0
    )
    if int(existing_count) >= duplicate_hash_limit:
        raise HTTPException(
            status_code=429,
            detail="Duplicate business card submissions exceeded for this time window",
        )


def _audit_event(
    session: Session,
    *,
    actor: dict,
    event_type: str,
    target_type: str,
    target_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    session.add(
        NetworkAuditEvent(
            id=uuid.uuid4(),
            actor_user_id=_actor_user_id(actor) or None,
            actor_email=actor.get("email"),
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata or {},
        )
    )


def _event_source_file() -> Path:
    # /.../CodeCollective/portal/org-backend/org.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "baltimore" / "event_sources.py"


def _load_event_sources() -> List[Dict[str, Any]]:
    file_path = _event_source_file()
    if not file_path.exists():
        logger.warning(f"Event source file not found: {file_path}")
        return []
    try:
        source_code = file_path.read_text(encoding="utf-8")
        parsed = ast.parse(source_code, filename=str(file_path))
        sources: list[dict[str, Any]] = []
        for node in parsed.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "sources":
                        value = ast.literal_eval(node.value)
                        if isinstance(value, list):
                            for item in value:
                                if isinstance(item, dict):
                                    sources.append(item)
        return sources
    except Exception as exc:
        logger.error(f"Failed to parse event sources: {exc}")
        return []


def _seed_organizations_from_event_sources(session: Session, force_update: bool = False) -> SeedOrganizationsResponse:
    sources = _load_event_sources()
    inserted = 0
    updated = 0

    for src in sources:
        name = str(src.get("name") or "").strip()
        source_url = str(src.get("url") or "").strip() or None
        if not name:
            continue
        tags = src.get("tags") if isinstance(src.get("tags"), list) else []
        image_url = str(src.get("orgImageUrl") or "").strip() or None

        org = _find_org_by_source_url(session, source_url)
        if not org:
            slug = _ensure_unique_org_slug(session, name)
            org = Organization(
                id=uuid.uuid4(),
                name=name,
                slug=slug,
                description=f"Seeded from Code Collective events source: {name}",
                source_url=source_url,
                source_urls=[source_url] if source_url else [],
                image_url=image_url,
                tags=tags,
                seeded_from_events=True,
            )
            session.add(org)
            inserted += 1
            continue

        if force_update:
            org.name = name
            _add_org_source_url(org, source_url)
            if image_url:
                org.image_url = image_url
            if tags:
                org.tags = tags
            org.seeded_from_events = True
            org.updated_at = datetime.now(timezone.utc)
            updated += 1

    session.commit()
    return SeedOrganizationsResponse(loaded=len(sources), inserted=inserted, updated=updated)


def _is_org_admin(org: Organization, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    current_user_id = _actor_user_id(current_user)
    if not current_user_id:
        return False
    if org.claimed_by_user_id == current_user_id:
        return True
    for membership in org.memberships or []:
        if membership.user_id == current_user_id and membership.role == "admin":
            return True
    return False


def _can_manage_org_for_merge(org: Organization, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    # For unclaimed organizations, any authenticated user can fold duplicates into
    # an org they already manage.
    if not org.claimed_by_user_id:
        return True
    return _is_org_admin(org, current_user)


def _is_any_org_admin(session: Session, current_user: dict) -> bool:
    if _is_sysadmin(current_user):
        return True
    user_id = _actor_user_id(current_user)
    if not user_id:
        return False
    claimed_org = (
        session.query(Organization.id)
        .filter(Organization.claimed_by_user_id == user_id)
        .first()
    )
    if claimed_org:
        return True
    member_admin = (
        session.query(OrganizationMembership.id)
        .filter(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == "admin",
        )
        .first()
    )
    return bool(member_admin)


def _has_active_team_membership(session: Session, user_id: str) -> bool:
    if not user_id:
        return False
    row = (
        session.query(TeamMembership.id)
        .join(Team, Team.id == TeamMembership.team_id)
        .filter(
            TeamMembership.user_id == user_id,
            TeamMembership.active.is_(True),
            Team.status == "active",
        )
        .first()
    )
    return bool(row)


def _has_recent_attendance(session: Session, user_id: str, days: int = 90) -> bool:
    if not user_id:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    row = (
        session.query(EventAttendance.id)
        .filter(
            EventAttendance.user_id == user_id,
            EventAttendance.attended_at >= cutoff,
        )
        .first()
    )
    return bool(row)


def _resolve_access_classes(session: Session, current_user: dict) -> AccessClassSnapshotResponse:
    if current_user.get("is_anonymous"):
        return AccessClassSnapshotResponse(
            is_public=True,
            is_attendee=False,
            is_member=False,
            is_org_admin=False,
            is_sysadmin=False,
            reasons=["Unauthenticated user defaults to Public class."],
        )

    user_id = _actor_user_id(current_user)
    is_sysadmin = _is_sysadmin(current_user)
    is_org_admin = _is_any_org_admin(session, current_user)
    is_attendee = _has_recent_attendance(session, user_id, days=90)
    is_member = is_org_admin or _has_active_team_membership(session, user_id)
    reasons: list[str] = []
    if is_sysadmin:
        reasons.append("Platform SysAdmin privileges are active.")
    if is_org_admin:
        reasons.append("User has organization admin authority (owner/membership or SysAdmin override).")
    if is_attendee:
        reasons.append("User has recorded attendance within the last 90 days.")
    if is_member:
        reasons.append("User is treated as Member due to org admin authority or active team participation.")
    if not reasons:
        reasons.append("Authenticated user defaults to Public class.")
    return AccessClassSnapshotResponse(
        is_public=True,
        is_attendee=is_attendee,
        is_member=is_member,
        is_org_admin=is_org_admin,
        is_sysadmin=is_sysadmin,
        reasons=reasons,
    )


def _claim_org_record(session: Session, org: Organization, current_user: dict) -> None:
    user_id = _actor_user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if org.claimed_by_user_id and org.claimed_by_user_id != user_id:
        raise HTTPException(status_code=409, detail="Organization is already claimed")

    org.claimed_by_user_id = user_id
    if not org.created_by_user_id:
        org.created_by_user_id = user_id

    membership = (
        session.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == user_id,
        )
        .first()
    )
    if not membership:
        membership = OrganizationMembership(
            id=uuid.uuid4(),
            organization=org,
            user_id=user_id,
            user_email=current_user.get("email"),
            user_name=current_user.get("name"),
            role="admin",
        )
        session.add(membership)
    else:
        membership.role = "admin"
        membership.user_email = current_user.get("email")
        membership.user_name = current_user.get("name")

    _audit_event(
        session,
        actor=current_user,
        event_type="org.claimed",
        target_type="organization",
        target_id=str(org.id),
        metadata={"claimed_by": user_id},
    )
