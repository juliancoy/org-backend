import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


# ============= AUTHENTICATION =============

def _is_sysadmin(current_user: dict) -> bool:
    return bool(current_user.get("is_sysadmin"))


async def _fetch_pidp_identity(token: str) -> dict[str, Any]:
    timeout = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)
    auth_headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        auth_resp = await client.get(
            f"{PIDP_BASE_URL}/auth/me",
            headers=auth_headers,
        )

        if auth_resp.is_success:
            pidp_user = auth_resp.json()
            user_id = str(pidp_user.get("id") or "").strip()
            email = str(pidp_user.get("email") or "").strip()
            name = str(pidp_user.get("full_name") or email or "User").strip()
            pidp_is_sysadmin = bool(pidp_user.get("is_sysadmin"))
            if not user_id or not email:
                raise HTTPException(status_code=401, detail="Invalid credentials")
            return {
                "pidp_id": user_id,
                "email": email,
                "name": name,
                "pidp_is_sysadmin": pidp_is_sysadmin,
                "token_kind": "jwt",
                "token_scope": "session",
                "token_scope_grants": ["session:*"],
            }

        service_resp = await client.get(
            f"{PIDP_BASE_URL}/service/token-info",
            headers=auth_headers,
        )
        if not service_resp.is_success:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        token_info = service_resp.json()
        token_kind = str(token_info.get("token_kind") or "").strip().lower()
        scope = str(token_info.get("scope") or "").strip()
        owner = token_info.get("owner") or {}
        user_id = str(owner.get("id") or "").strip()
        email = str(owner.get("email") or "").strip()
        name = str(owner.get("full_name") or email or "User").strip()
        pidp_is_sysadmin = bool(owner.get("is_sysadmin"))
        if not user_id or not email:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if token_kind == "pat":
            if not scope or scope not in ORG_ALLOWED_PAT_SCOPES:
                allowed = ", ".join(sorted(ORG_ALLOWED_PAT_SCOPES))
                raise HTTPException(
                    status_code=403,
                    detail=f"PAT scope '{scope or 'none'}' is not allowed for Org backend. Allowed scopes: {allowed}",
                )

        return {
            "pidp_id": user_id,
            "email": email,
            "name": name,
            "pidp_is_sysadmin": pidp_is_sysadmin,
            "token_kind": token_kind or "unknown",
            "token_scope": scope or "unknown",
            "token_scope_grants": token_info.get("scope_grants") or [],
        }


def _extract_pidp_avatar_url(user_row: dict[str, Any]) -> str | None:
    if not isinstance(user_row, dict):
        return None
    identity_data = user_row.get("identity_data")
    if not isinstance(identity_data, dict):
        identity_data = {}
    candidates = [
        user_row.get("avatar_url"),
        identity_data.get("avatar_url"),
        user_row.get("picture"),
        user_row.get("photo_url"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return None


async def _fetch_pidp_avatar_map_by_email(token: str, emails: set[str]) -> dict[str, str]:
    target_emails = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
    if not target_emails:
        return {}

    resolved: dict[str, str] = {}
    timeout = httpx.Timeout(connect=10.0, read=12.0, write=10.0, pool=10.0)
    headers = {"Authorization": f"Bearer {token}"}
    limit = max(100, min(len(target_emails), 500))
    offset = 0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for _ in range(20):
                resp = await client.get(
                    f"{PIDP_BASE_URL}/auth/users",
                    params={"limit": limit, "offset": offset},
                    headers=headers,
                )
                if not resp.is_success:
                    # This endpoint is often restricted to sysadmin tokens.
                    return resolved

                payload = resp.json()
                rows: list[dict[str, Any]]
                total: int | None = None
                if isinstance(payload, list):
                    rows = [row for row in payload if isinstance(row, dict)]
                elif isinstance(payload, dict):
                    raw_rows = payload.get("users")
                    if not isinstance(raw_rows, list):
                        raw_rows = payload.get("items")
                    if not isinstance(raw_rows, list):
                        raw_rows = payload.get("results")
                    rows = [row for row in (raw_rows or []) if isinstance(row, dict)]
                    try:
                        total = int(payload.get("total")) if payload.get("total") is not None else None
                    except Exception:
                        total = None
                else:
                    rows = []

                if not rows:
                    break

                for row in rows:
                    email = str(row.get("email") or "").strip().lower()
                    if not email or email not in target_emails or email in resolved:
                        continue
                    avatar_url = _extract_pidp_avatar_url(row)
                    if avatar_url:
                        resolved[email] = avatar_url

                if len(resolved) >= len(target_emails):
                    break

                page_count = len(rows)
                if page_count < limit:
                    break
                offset += page_count
                if total is not None and offset >= total:
                    break
    except Exception as exc:
        logger.warning(f"Unable to load PIdP avatars for network users: {exc}")

    return resolved


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_db)
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    token = credentials.credentials
    
    try:
        identity = await _fetch_pidp_identity(token)
        user_id = identity["pidp_id"]
        email = identity["email"]
        name = identity["name"]

        # Find or create account
        account = session.query(Account).filter_by(email=email).first()
        if not account:
            account = Account(
                id=uuid.uuid4(),
                entity_type=EntityType.INDIVIDUAL,
                name=name,
                email=email,
                balance=Decimal('10000.00')
            )
            session.add(account)
            session.commit()

        # Prefer PIdP-provided platform admin claim as the source of truth.
        # Keep local checks as break-glass fallback for recovery/bootstrap.
        pidp_is_sysadmin = bool(identity.get("pidp_is_sysadmin"))
        break_glass_is_sysadmin = user_id in ORG_SYSADMIN_USER_IDS or await _spicedb_check_sysadmin(user_id)
        is_sysadmin = pidp_is_sysadmin or break_glass_is_sysadmin
        return {
            "id": str(account.id),
            "email": account.email,
            "name": account.name,
            "is_anonymous": False,
            "is_sysadmin": is_sysadmin,
            "pidp_id": user_id,
            "token_kind": identity.get("token_kind"),
            "token_scope": identity.get("token_scope"),
            "token_scope_grants": identity.get("token_scope_grants") or [],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/auth/social/{provider}/login")
async def redirect_social_login(
    provider: str,
    request: Request,
    next: Optional[str] = None,
):
    normalized_provider = (provider or "").strip().lower()
    if normalized_provider not in {"google", "github"}:
        raise HTTPException(status_code=404, detail="Unsupported social provider")

    next_target = (next or "").strip()
    if not next_target:
        next_target = str(request.headers.get("referer") or "").strip() or "/"

    query = {"app": PIDP_APP_SLUG}
    if next_target:
        query["next"] = next_target
    pidp_login_url = f"{PIDP_BASE_URL}/auth/{normalized_provider}/login?{urlencode(query)}"
    return RedirectResponse(url=pidp_login_url, status_code=307)


def _require_authenticated_user(current_user: dict) -> None:
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")


def _actor_user_id(current_user: dict) -> str:
    return str(current_user.get("pidp_id") or current_user.get("id") or "")


def _token_grants(current_user: dict) -> set[str]:
    grants = current_user.get("token_scope_grants") or []
    normalized: set[str] = set()
    for grant in grants:
        value = str(grant or "").strip()
        if value:
            normalized.add(value)
    return normalized


def _has_required_pat_grant(current_user: dict, required_grants: list[str]) -> bool:
    if not required_grants:
        return True
    token_kind = str(current_user.get("token_kind") or "").strip().lower()
    if token_kind != "pat":
        return True

    grants = _token_grants(current_user)
    if "*" in grants:
        return True

    def _grant_allows(token_grant: str, required_grant: str) -> bool:
        if token_grant == "*":
            return True
        if token_grant.endswith("*"):
            return required_grant.startswith(token_grant[:-1])
        return token_grant == required_grant

    for required in required_grants:
        required_value = str(required or "").strip()
        if not required_value:
            continue
        if any(_grant_allows(token_grant, required_value) for token_grant in grants):
            return True
    return False


def _require_sysadmin(
    current_user: dict,
    *,
    pat_required_grants: Optional[list[str]] = None,
    detail: str = "SysAdmin privileges required",
) -> None:
    if not _is_sysadmin(current_user):
        raise HTTPException(status_code=403, detail=detail)
    if pat_required_grants and not _has_required_pat_grant(current_user, pat_required_grants):
        raise HTTPException(
            status_code=403,
            detail=f"PAT missing required grant. Need one of: {', '.join(pat_required_grants)}",
        )


def _can_use_sysadmin_override(current_user: dict, required_grants: Optional[list[str]] = None) -> bool:
    if not _is_sysadmin(current_user):
        return False
    if required_grants and not _has_required_pat_grant(current_user, required_grants):
        return False
    return True


def _normalize_business_card_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    def _lineify_json_entities(parsed: Any) -> str:
        if not isinstance(parsed, list):
            return ""
        lines: list[str] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entity_type = str(item.get("type") or "").strip().lower()
            if entity_type in {"person", "organization", "event"}:
                lines.append(entity_type)
            for key in (
                "name",
                "title",
                "company",
                "email",
                "phone",
                "website",
                "address",
                "description",
                "location",
                "starts_at",
            ):
                candidate = item.get(key)
                if candidate is None:
                    continue
                text = str(candidate).strip()
                if text:
                    lines.append(text)
        return "\n".join(lines)

    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            normalized_from_json = _lineify_json_entities(parsed)
            if normalized_from_json:
                raw = normalized_from_json
        except Exception:
            pass

    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join([line for line in lines if line])


def _extract_business_card_fields(ocr_text: str) -> dict[str, Any]:
    text = _normalize_business_card_text(ocr_text)
    lines = text.splitlines()
    lowered = [line.lower() for line in lines]

    email_match = re.search(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.IGNORECASE)
    phone_match = re.search(r"(?:\+?\d[\d\-\(\)\s]{7,}\d)", text)
    website_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", text, re.IGNORECASE)

    likely_name = lines[0] if lines else None
    likely_company = None
    likely_title = None
    title_keywords = {
        "ceo", "cto", "cfo", "coo", "founder", "manager", "director", "lead", "engineer",
        "president", "partner", "owner", "consultant", "developer", "designer",
    }
    for idx, line in enumerate(lines[1:5], start=1):
        tokens = set(token.strip(",. ").lower() for token in line.split())
        if tokens & title_keywords and not likely_title:
            likely_title = line
            continue
        if "@" not in line and not re.search(r"\d", line) and not likely_company:
            likely_company = line
        if likely_company and likely_title:
            break
        if idx > 4:
            break

    address_parts = []
    for idx, line in enumerate(lines):
        if re.search(r"\d{5}(?:-\d{4})?$", line) or any(
            token in lowered[idx] for token in ["street", "st.", "avenue", "ave", "road", "rd.", "suite", "blvd", "drive"]
        ):
            address_parts.append(line)
    address = ", ".join(address_parts) if address_parts else None

    return {
        "name": likely_name,
        "title": likely_title,
        "company": likely_company,
        "email": email_match.group(0).strip() if email_match else None,
        "phone": phone_match.group(0).strip() if phone_match else None,
        "website": website_match.group(0).strip() if website_match else None,
        "address": address,
        "raw_lines": lines,
    }


def _coerce_public_url_candidate(value: Optional[str], field_name: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("www."):
        raw = f"https://{raw}"
    try:
        return _validate_public_url(raw, field_name)
    except HTTPException:
        return None


def _parse_scan_datetime_candidate(value: str) -> Optional[datetime]:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    candidate = re.sub(r"\s+", " ", candidate.replace(" at ", " ").strip())
    formats = [
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(candidate, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _extract_public_urls_from_text(text: str, max_urls: int = 10) -> list[str]:
    if max_urls < 1:
        return []
    candidates = re.findall(r"(https?://[^\s<>'\"()]+|www\.[^\s<>'\"()]+)", text or "", flags=re.IGNORECASE)

    def _canonicalize_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or ""
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        if not path:
            path = "/"
        return parsed._replace(
            netloc=host,
            path=path,
            fragment="",
            params="",
            query=parsed.query or "",
        ).geturl()

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _coerce_public_url_candidate(candidate, "source_url")
        if not normalized:
            continue
        normalized = _canonicalize_url(normalized)
        key = normalized.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(normalized)
        if len(urls) >= max_urls:
            break
    return urls


def _collect_event_links_from_html(source_url: str, html_text: str, max_links: int = 15) -> list[str]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(source_url, href)
        normalized = _coerce_public_url_candidate(absolute, "source_url")
        if not normalized:
            continue
        label = (anchor.get_text(" ", strip=True) or "").lower()
        href_lower = normalized.lower()
        if (
            "/event" not in href_lower
            and "register" not in href_lower
            and "ticket" not in href_lower
            and "event" not in label
            and "register" not in label
            and "ticket" not in label
            and "meetup" not in label
        ):
            continue
        key = normalized.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        links.append(normalized)
        if len(links) >= max_links:
            break
    return links


def _extract_event_candidate_from_jsonld(source_url: str, html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw_json = (script.string or script.get_text(strip=True) or "").strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
        except Exception:
            continue
        nodes: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            if isinstance(parsed.get("@graph"), list):
                nodes.extend([item for item in parsed["@graph"] if isinstance(item, dict)])
            else:
                nodes.append(parsed)
        elif isinstance(parsed, list):
            nodes.extend([item for item in parsed if isinstance(item, dict)])
        for node in nodes:
            node_type = str(node.get("@type") or "").lower()
            if "event" not in node_type:
                continue
            location_value = node.get("location")
            location_name = None
            if isinstance(location_value, dict):
                location_name = str(location_value.get("name") or location_value.get("address") or "").strip() or None
            elif isinstance(location_value, str):
                location_name = location_value.strip() or None
            return {
                "title": str(node.get("name") or "").strip() or None,
                "description": str(node.get("description") or "").strip() or None,
                "starts_at": _coerce_calendar_datetime(node.get("startDate")),
                "location": location_name,
                "source_url": _coerce_public_url_candidate(node.get("url"), "source_url")
                or _coerce_public_url_candidate(source_url, "source_url"),
            }
    return {}


async def _enrich_event_scan_from_links(
    *,
    ocr_text: str,
    seed_url: Optional[str],
    max_fetches: int = 3,
    max_links: int = 20,
) -> dict[str, Any]:
    source_candidates: list[str] = []
    if seed_url:
        normalized_seed = _coerce_public_url_candidate(seed_url, "source_url")
        if normalized_seed:
            source_candidates.append(normalized_seed)
    source_candidates.extend(_extract_public_urls_from_text(ocr_text, max_urls=max_links))

    deduped_candidates: list[str] = []
    seen_candidates: set[str] = set()
    for candidate in source_candidates:
        key = candidate.strip().lower()
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        deduped_candidates.append(candidate)

    visited_urls: list[str] = []
    discovered_links: list[str] = []
    event_candidate: dict[str, Any] = {}
    discovered_link_keys: set[str] = set()
    for candidate in deduped_candidates[:max_fetches]:
        safe_url = _ensure_public_fetch_url(candidate, "event_link")
        visited_urls.append(safe_url)
        timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                safe_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "ArkavoOrgScanner/1.0",
                },
            )
        if not response.is_success:
            continue
        content_type = str(response.headers.get("content-type") or "").lower()
        if "html" not in content_type:
            continue
        html = response.text
        resolved_url = _coerce_public_url_candidate(str(response.url), "source_url") or safe_url
        candidate_from_jsonld = _extract_event_candidate_from_jsonld(resolved_url, html)
        if candidate_from_jsonld and not event_candidate:
            event_candidate = candidate_from_jsonld
        for link in _collect_event_links_from_html(resolved_url, html, max_links=max_links):
            key = link.lower()
            if key in discovered_link_keys:
                continue
            discovered_link_keys.add(key)
            discovered_links.append(link)
        if len(discovered_links) >= max_links:
            discovered_links = discovered_links[:max_links]
            break

    return {
        "visited_urls": visited_urls,
        "discovered_links": discovered_links[:max_links],
        "event_candidate": event_candidate,
    }


def _extract_event_fields_from_text(ocr_text: str) -> dict[str, Any]:
    events = _extract_events_from_text(ocr_text)
    if events:
        return events[0]

    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {"title": None, "starts_at": None, "location": None, "website": None, "description": None, "raw_lines": lines}


def _extract_events_from_text(ocr_text: str) -> list[dict[str, Any]]:
    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    extracted_urls = _extract_public_urls_from_text(text, max_urls=5)
    month_hint = re.compile(
        r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
        re.IGNORECASE,
    )
    time_hint = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.IGNORECASE)
    date_hint = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}", re.IGNORECASE)
    noisy_location_tokens = (
        "we look forward",
        "open houses",
        "open house",
        "transit administration",
        "department of transportation",
    )

    title = None
    for line in lines:
        lowered = line.lower()
        if "@" in lowered:
            continue
        if re.search(r"(https?://|www\.)", lowered):
            continue
        if re.search(r"\+?\d[\d\-\(\)\s]{7,}\d", line):
            continue
        if time_hint.search(lowered):
            continue
        if month_hint.search(lowered) or date_hint.search(lowered):
            continue
        title = line
        break
    title = title or lines[0]

    def _is_date_line(value: str) -> bool:
        normalized = value.strip().replace("–", "-").replace("—", "-")
        return bool(month_hint.search(normalized) or date_hint.search(normalized))

    def _parse_event_start_at(date_line: str, next_line: Optional[str]) -> Optional[datetime]:
        date_candidate = date_line.strip().replace("–", "-").replace("—", "-").rstrip(":").strip()
        starts_at = _parse_scan_datetime_candidate(date_candidate)
        if starts_at:
            return starts_at
        if not next_line:
            return None
        next_normalized = next_line.strip().replace("–", "-").replace("—", "-")
        time_match = re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", next_normalized, flags=re.IGNORECASE)
        if not time_match:
            return None
        combined = f"{date_candidate} {time_match.group(0)}"
        return _parse_scan_datetime_candidate(combined)

    events: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not _is_date_line(line):
            idx += 1
            continue
        next_line = lines[idx + 1] if (idx + 1) < len(lines) else None
        starts_at = _parse_event_start_at(line, next_line)
        if not starts_at:
            idx += 1
            continue

        location_start = idx + 1
        if next_line and time_hint.search(next_line):
            location_start = idx + 2
        location_parts: list[str] = []
        cursor = location_start
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            lowered = candidate.lower()
            if _is_date_line(candidate):
                break
            if re.search(r"(https?://|www\.)", lowered):
                break
            if any(token in lowered for token in noisy_location_tokens):
                break
            if time_hint.search(lowered):
                cursor += 1
                continue
            location_parts.append(candidate)
            if len(location_parts) >= 2:
                break
            cursor += 1

        location = " ".join(location_parts).strip() or None
        event_payload = {
            "title": title,
            "starts_at": starts_at,
            "location": location,
            "website": extracted_urls[0] if extracted_urls else None,
            "links": extracted_urls,
            "description": None,
            "raw_lines": lines,
        }
        events.append(event_payload)
        idx = max(idx + 1, cursor)

    if events:
        return events

    # Fallback to single-event heuristics for less structured OCR.
    starts_at: Optional[datetime] = None
    for line in lines:
        normalized = line.strip().replace("–", "-").replace("—", "-")
        if _is_date_line(normalized):
            starts_at = _parse_scan_datetime_candidate(normalized.split(" - ", 1)[0].strip())
            if starts_at:
                break
    location = None
    location_markers = ("location", "venue", "address", "st.", "street", "ave", "avenue", "blvd", "road", "rd.")
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in location_markers):
            location = line.split(":", 1)[-1].strip() if ":" in line else line
            break
    description_lines = [line for line in lines[1:5] if line != location]
    description = "\n".join(description_lines).strip() or None
    if description and len(description) > 2000:
        description = description[:2000]
    return [
        {
            "title": title,
            "starts_at": starts_at,
            "location": location,
            "website": extracted_urls[0] if extracted_urls else None,
            "links": extracted_urls,
            "description": description,
            "raw_lines": lines,
        }
    ]


def _extract_organization_fields_from_text(ocr_text: str) -> dict[str, Any]:
    text = _normalize_business_card_text(ocr_text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {"name": None, "website": None, "description": None, "raw_lines": []}

    website_match = re.search(r"(https?://[^\s]+|www\.[^\s]+)", text, re.IGNORECASE)
    name = None
    org_tokens = ("inc", "llc", "ltd", "foundation", "association", "collective", "organization", "org", "corp", "company")
    for line in lines:
        lowered = line.lower()
        if re.search(r"(https?://|www\.)", lowered):
            continue
        if "@" in lowered:
            continue
        if any(token in lowered for token in org_tokens):
            name = line
            break
    if not name:
        name = lines[0]

    description_lines = [line for line in lines[1:5] if "@" not in line and not re.search(r"(https?://|www\.)", line, re.IGNORECASE)]
    description = "\n".join(description_lines).strip() or None
    if description and len(description) > 2000:
        description = description[:2000]

    return {
        "name": name,
        "website": website_match.group(0).strip() if website_match else None,
        "description": description,
        "raw_lines": lines,
    }


def _derive_org_name_from_website(website: Optional[str]) -> Optional[str]:
    raw = str(website or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("www."):
        raw = f"https://{raw}"
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".", 1)[0].strip()
    if not label:
        return None
    label = re.sub(r"[-_]+", " ", label)
    label = re.sub(r"\s+", " ", label).strip()
    if not label:
        return None
    return label.title()


def _derive_org_payload_for_person_scan(
    extracted_person: dict[str, Any],
    extracted_org: dict[str, Any],
) -> Optional[dict[str, Any]]:
    person_name = str(extracted_person.get("name") or "").strip()
    person_name_key = re.sub(r"\s+", " ", person_name).strip().lower()

    candidate_names: list[str] = []
    for candidate in [
        extracted_person.get("company"),
        extracted_org.get("name"),
    ]:
        value = str(candidate or "").strip()
        if not value:
            continue
        value_key = re.sub(r"\s+", " ", value).strip().lower()
        if person_name_key and value_key == person_name_key:
            continue
        if value not in candidate_names:
            candidate_names.append(value)

    website = (
        _coerce_public_url_candidate(extracted_org.get("website"), "source_url")
        or _coerce_public_url_candidate(extracted_person.get("website"), "source_url")
    )
    chosen_name = candidate_names[0] if candidate_names else None
    if not chosen_name and website:
        chosen_name = _derive_org_name_from_website(website)
    if not chosen_name:
        return None
    return {
        "name": chosen_name,
        "website": website,
        "description": extracted_org.get("description"),
        "raw_lines": extracted_org.get("raw_lines") or extracted_person.get("raw_lines") or [],
    }


def _normalize_scan_kind(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"person", "organization", "event", "auto"}:
        return normalized
    return "auto"


def _score_scan_kind_candidates(
    ocr_text: str,
    extracted_person: dict[str, Any],
    extracted_org: dict[str, Any],
    extracted_event: dict[str, Any],
) -> dict[str, float]:
    text = _normalize_business_card_text(ocr_text).lower()

    person_score = 0.0
    if extracted_person.get("email"):
        person_score += 0.65
    if extracted_person.get("phone"):
        person_score += 0.2
    if extracted_person.get("name"):
        person_score += 0.1
    if extracted_person.get("title") or extracted_person.get("company"):
        person_score += 0.05

    organization_score = 0.0
    if extracted_org.get("name"):
        organization_score += 0.55
    if extracted_org.get("website"):
        organization_score += 0.25
    if extracted_org.get("description"):
        organization_score += 0.1
    if any(token in text for token in ["org", "organization", "inc", "llc", "nonprofit", "foundation"]):
        organization_score += 0.1

    event_score = 0.0
    if extracted_event.get("starts_at"):
        event_score += 0.55
    if extracted_event.get("website"):
        event_score += 0.2
    if extracted_event.get("location"):
        event_score += 0.15
    if any(token in text for token in ["event", "meetup", "conference", "workshop", "summit", "webinar"]):
        event_score += 0.1

    return {
        "person": max(0.0, min(1.0, person_score)),
        "organization": max(0.0, min(1.0, organization_score)),
        "event": max(0.0, min(1.0, event_score)),
    }


def _detect_scan_kind(ocr_text: str, extracted_person: dict[str, Any], extracted_org: dict[str, Any], extracted_event: dict[str, Any]) -> str:
    scores = _score_scan_kind_candidates(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return sorted_scores[0][0] if sorted_scores else "person"


def _create_or_find_organization_from_scan(
    session: Session,
    *,
    actor_user_id: str,
    extracted_org: dict[str, Any],
    default_image_url: Optional[str] = None,
) -> Organization:
    name = str(extracted_org.get("name") or "").strip() or "Organization"
    source_url = _coerce_public_url_candidate(extracted_org.get("website"), "source_url")
    if source_url:
        existing = _find_org_by_source_url(session, source_url)
        if existing:
            if default_image_url and not (existing.image_url or "").strip():
                existing.image_url = default_image_url
            return existing
    existing_by_name = session.query(Organization).filter(func.lower(Organization.name) == name.lower()).first()
    if existing_by_name:
        if source_url:
            _add_org_source_url(existing_by_name, source_url)
        if default_image_url and not (existing_by_name.image_url or "").strip():
            existing_by_name.image_url = default_image_url
        return existing_by_name
    normalized_slug = _slugify(name)
    if normalized_slug:
        existing_by_slug = session.query(Organization).filter(Organization.slug == normalized_slug).first()
        if existing_by_slug:
            if source_url:
                _add_org_source_url(existing_by_slug, source_url)
            if default_image_url and not (existing_by_slug.image_url or "").strip():
                existing_by_slug.image_url = default_image_url
            return existing_by_slug

    org = Organization(
        id=uuid.uuid4(),
        name=name[:255],
        slug=_ensure_unique_org_slug(session, name),
        description=(str(extracted_org.get("description") or "").strip() or None),
        source_url=source_url,
        source_urls=[source_url] if source_url else [],
        image_url=default_image_url,
        tags=["source:scan"],
        seeded_from_events=False,
        claimed_by_user_id=None,
        created_by_user_id=actor_user_id,
    )
    session.add(org)
    return org


def _create_event_from_scan(
    session: Session,
    *,
    actor_user_id: str,
    extracted_event: dict[str, Any],
    default_image_url: Optional[str] = None,
) -> NetworkEvent:
    title = str(extracted_event.get("title") or "").strip() or "Untitled Event"
    starts_at = extracted_event.get("starts_at")
    location = str(extracted_event.get("location") or "").strip() or None
    source_url = _coerce_public_url_candidate(extracted_event.get("website"), "source_url")
    if source_url:
        existing = session.query(NetworkEvent).filter(NetworkEvent.source_url == source_url).first()
        if existing:
            if default_image_url and not (existing.image_url or "").strip():
                existing.image_url = default_image_url
            return existing
    duplicate_query = session.query(NetworkEvent).filter(func.lower(NetworkEvent.title) == title.lower())
    if starts_at is not None:
        duplicate_query = duplicate_query.filter(NetworkEvent.starts_at == starts_at)
    if location:
        duplicate_query = duplicate_query.filter(func.lower(NetworkEvent.location) == location.lower())
    existing_structured = duplicate_query.order_by(NetworkEvent.created_at.desc()).first()
    if existing_structured:
        if default_image_url and not (existing_structured.image_url or "").strip():
            existing_structured.image_url = default_image_url
        return existing_structured

    event = NetworkEvent(
        id=uuid.uuid4(),
        title=title[:255],
        slug=_ensure_unique_event_slug(session, title),
        description=(str(extracted_event.get("description") or "").strip() or None),
        starts_at=starts_at,
        ends_at=None,
        location=location,
        source_url=source_url,
        image_url=default_image_url,
        tags=["source:scan"],
        host_type=EventHostType.UNCLAIMED.value,
        host_user_id=None,
        host_org_id=None,
        claimed_by_user_id=None,
        created_by_user_id=actor_user_id,
        seeded_from_events=False,
    )
    session.add(event)
    return event


async def _ocr_business_card_with_openai(
    image_bytes: bytes,
    content_type: str,
    *,
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> str:
    return await _svc_ocr_business_card_with_openai(
        image_bytes=image_bytes,
        content_type=content_type,
        api_key=ORG_OPENAI_API_KEY,
        api_base_url=ORG_OPENAI_API_BASE_URL,
        model=ORG_BUSINESS_CARD_OCR_MODEL,
        normalize_text=_normalize_business_card_text,
        audit_hook=audit_hook,
    )


def _extract_text_content_from_openai_message_content(content: Any) -> str:
    return _svc_extract_text_content_from_openai_message_content(content)


async def _summarize_scan_targets_with_openai(
    *,
    ocr_text: str,
    created_targets: List[Dict[str, Optional[str]]],
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Optional[str]]]:
    return await _svc_summarize_scan_targets_with_openai(
        ocr_text=ocr_text,
        created_targets=created_targets,
        summary_enabled=ORG_SCAN_AI_SUMMARY_ENABLED,
        api_key=ORG_OPENAI_API_KEY,
        api_base_url=ORG_OPENAI_API_BASE_URL,
        model=ORG_BUSINESS_CARD_OCR_MODEL,
        audit_hook=audit_hook,
    )


async def _extract_business_card_text(
    image_bytes: bytes,
    content_type: str,
    *,
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> tuple[str, str]:
    provider = ORG_BUSINESS_CARD_OCR_PROVIDER
    if provider == "openai":
        return await _ocr_business_card_with_openai(
            image_bytes=image_bytes,
            content_type=content_type,
            audit_hook=audit_hook,
        ), provider
    raise HTTPException(status_code=503, detail=f"Unsupported ORG_BUSINESS_CARD_OCR_PROVIDER '{provider}'")


async def _create_or_find_pidp_user_from_business_card(email: str, full_name: Optional[str]) -> dict[str, Any]:
    generated_password = secrets.token_urlsafe(24)
    payload = {
        "email": email,
        "password": generated_password,
        "full_name": full_name or None,
    }
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{PIDP_BASE_URL}/auth/register",
            json=payload,
        )
    if response.status_code in {200, 201}:
        data = response.json()
        return {
            "created": True,
            "pidp_user_id": str(data.get("id") or ""),
            "generated_password": generated_password,
        }
    if response.status_code == 409:
        return {
            "created": False,
            "pidp_user_id": None,
            "generated_password": None,
        }
    detail = response.text.strip() or f"Unable to create PIdP user ({response.status_code})"
    raise HTTPException(status_code=502, detail=detail)


def _send_business_card_added_email(
    recipient_email: str,
    recipient_name: Optional[str],
    submitted_by_name: Optional[str],
) -> None:
    if not ORG_SMTP_HOST:
        raise RuntimeError("SMTP is not configured (ORG_SMTP_HOST missing)")

    portal_url = ORG_PORTAL_BASE_URL or "https://portal.arkavo.org"
    display_name = (recipient_name or recipient_email).strip()
    submitter = (submitted_by_name or "an org admin").strip()

    msg = EmailMessage()
    msg["Subject"] = "You were added to Arkavo OrgPortal"
    msg["From"] = ORG_SMTP_FROM
    msg["To"] = recipient_email
    msg.set_content(
        (
            f"Hi {display_name},\n\n"
            f"{submitter} submitted your business card and added you to Arkavo OrgPortal.\n"
            f"You can sign in or register with this email at:\n{portal_url}\n\n"
            "If this was unexpected, please ignore this email.\n"
        )
    )

    with smtplib.SMTP(ORG_SMTP_HOST, ORG_SMTP_PORT, timeout=30) as smtp:
        if ORG_SMTP_STARTTLS:
            smtp.starttls()
        if ORG_SMTP_USERNAME:
            smtp.login(ORG_SMTP_USERNAME, ORG_SMTP_PASSWORD)
        smtp.send_message(msg)


def _record_business_card_email_outcome(submission_id: uuid.UUID, sent: bool, error_message: Optional[str]) -> None:
    session = db.SessionLocal()
    try:
        record = session.query(BusinessCardSubmission).filter(BusinessCardSubmission.id == submission_id).first()
        if not record:
            return
        record.notification_email_sent = bool(sent)
        record.notification_error = error_message
        record.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _send_business_card_email_task(
    submission_id: uuid.UUID,
    recipient_email: str,
    recipient_name: Optional[str],
    submitted_by_name: Optional[str],
) -> None:
    try:
        _send_business_card_added_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            submitted_by_name=submitted_by_name,
        )
        _record_business_card_email_outcome(submission_id=submission_id, sent=True, error_message=None)
    except Exception as exc:
        _record_business_card_email_outcome(submission_id=submission_id, sent=False, error_message=str(exc))


