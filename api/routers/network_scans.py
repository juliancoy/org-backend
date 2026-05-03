import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from fastapi import APIRouter

router = APIRouter(tags=["network-scans"])



@router.post("/api/network/scans", response_model=BusinessCardSubmissionResponse)
@router.post("/api/network/business-cards", response_model=BusinessCardSubmissionResponse)
async def submit_business_card(
    request: Request,
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    scan_kind: Optional[str] = Form("auto"),
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    submission_id = uuid.uuid4()
    actor_user_id = _actor_user_id(current_user)

    def _scan_audit(event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        _audit_event(
            session,
            actor=current_user,
            event_type=event_type,
            target_type="business_card_submission",
            target_id=str(submission_id),
            metadata=metadata or {},
        )

    ai_audit_steps: list[dict[str, Any]] = []

    def _scan_ai_audit_hook(event_type: str, metadata: Dict[str, Any]) -> None:
        payload = metadata or {}
        ai_audit_steps.append({"event_type": event_type, "metadata": payload})
        _scan_audit(event_type, payload)

    _scan_audit("scan.received", {"scan_kind_requested": _normalize_scan_kind(scan_kind)})
    runtime_settings = await get_business_card_runtime_settings()
    if not runtime_settings.get("enabled", True):
        raise HTTPException(status_code=503, detail="Business card submissions are temporarily disabled")

    client_ip = _request_client_ip(request)
    _throttle_action(
        f"network:business-card-submit:user:{actor_user_id}",
        limit=int(runtime_settings["per_user_limit_per_hour"]),
        window_seconds=3600,
    )
    _throttle_action(
        f"network:business-card-submit:ip:{client_ip}",
        limit=int(runtime_settings["per_ip_limit_per_hour"]),
        window_seconds=3600,
    )
    _throttle_action(
        "network:business-card-submit:global",
        limit=int(runtime_settings["global_limit_per_hour"]),
        window_seconds=3600,
    )

    content_type = (image.content_type or "").strip().lower()
    allowed_content_types = {
        item.strip().lower()
        for item in runtime_settings.get("allowed_content_types") or []
        if item and item.strip()
    } or ORG_BUSINESS_CARD_DEFAULT_ALLOWED_CONTENT_TYPES
    if content_type not in allowed_content_types:
        allowed = ", ".join(sorted(allowed_content_types))
        raise HTTPException(status_code=415, detail=f"Unsupported image content type '{content_type}'. Allowed: {allowed}")

    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Image file is empty")
    max_bytes = int(runtime_settings.get("max_bytes") or ORG_BUSINESS_CARD_DEFAULT_MAX_BYTES)
    if len(image_bytes) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Image exceeds {max_bytes} bytes")

    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if not _can_use_sysadmin_override(current_user, ["org:admin.write", "org:*"]):
        _enforce_business_card_duplicate_hash_guard(
            session,
            image_sha256=image_sha256,
            duplicate_hash_limit=int(runtime_settings["duplicate_hash_limit"]),
            duplicate_hash_window_seconds=int(runtime_settings["duplicate_hash_window_seconds"]),
        )
    image_storage_path = ""
    image_storage_backend = ""
    image_storage_bucket = None
    image_storage_error = None
    try:
        image_storage_backend, image_storage_bucket, image_storage_path = _persist_business_card_image(
            submission_id=submission_id,
            image_bytes=image_bytes,
            content_type=content_type,
        )
    except Exception as exc:
        image_storage_error = str(exc)
        logger.error("Business card image storage failed: %s", exc)
        raise HTTPException(status_code=503, detail="Unable to store business card image")
    _scan_audit(
        "scan.image.persisted",
        {
            "storage_backend": image_storage_backend,
            "storage_bucket": image_storage_bucket,
            "content_type": content_type,
            "image_size_bytes": len(image_bytes),
        },
    )

    ocr_text, ocr_provider = await _extract_business_card_text(
        image_bytes=image_bytes,
        content_type=content_type,
        audit_hook=_scan_ai_audit_hook,
    )
    extracted_person = _extract_business_card_fields(ocr_text)
    extracted_org = _extract_organization_fields_from_text(ocr_text)
    extracted_events = _extract_events_from_text(ocr_text)
    extracted_event = extracted_events[0] if extracted_events else _extract_event_fields_from_text(ocr_text)
    person_derived_org = _derive_org_payload_for_person_scan(extracted_person, extracted_org)
    normalized_scan_kind = _normalize_scan_kind(scan_kind)
    classification_scores = _score_scan_kind_candidates(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    ranked_scores = sorted(classification_scores.items(), key=lambda item: item[1], reverse=True)
    detected_scan_kind = _detect_scan_kind(
        ocr_text=ocr_text,
        extracted_person=extracted_person,
        extracted_org=extracted_org,
        extracted_event=extracted_event,
    )
    effective_scan_kind = detected_scan_kind if normalized_scan_kind == "auto" else normalized_scan_kind
    top_confidence = float(ranked_scores[0][1]) if ranked_scores else 0.0
    second_confidence = float(ranked_scores[1][1]) if len(ranked_scores) > 1 else 0.0
    confidence_margin = max(0.0, top_confidence - second_confidence)
    auto_clarification_enabled = bool(
        runtime_settings.get("auto_clarification_enabled", ORG_SCAN_AUTO_CLARIFICATION_ENABLED)
    )
    auto_min_confidence = max(
        0.0,
        min(1.0, float(runtime_settings.get("auto_min_confidence", ORG_SCAN_AUTO_MIN_CONFIDENCE))),
    )
    auto_min_margin = max(
        0.0,
        min(1.0, float(runtime_settings.get("auto_min_margin", ORG_SCAN_AUTO_MIN_MARGIN))),
    )
    high_confidence_scan_kinds = [
        kind for kind, score in classification_scores.items() if float(score or 0.0) >= auto_min_confidence
    ]
    auto_multi_entity_detected = normalized_scan_kind == "auto" and len(high_confidence_scan_kinds) > 1
    clarification_required = False
    clarification_message: Optional[str] = None
    processing_status = "processed"
    if (
        normalized_scan_kind == "auto"
        and auto_clarification_enabled
        and not auto_multi_entity_detected
        and (top_confidence < auto_min_confidence or confidence_margin < auto_min_margin)
    ):
        clarification_required = True
        processing_status = "clarification_required"
        clarification_message = (
            f"Scan classification confidence ({top_confidence:.2f}) was below policy. "
            "Please resubmit and select the correct scan type."
        )
        _scan_audit(
            "scan.classification.clarification_required",
            {
                "detected_scan_kind": detected_scan_kind,
                "classification_scores": classification_scores,
                "top_confidence": top_confidence,
                "confidence_margin": confidence_margin,
                "auto_min_confidence": auto_min_confidence,
                "auto_min_margin": auto_min_margin,
            },
        )
    event_link_enrichment_enabled = bool(runtime_settings.get("event_link_enrichment_enabled", ORG_SCAN_EVENT_LINK_ENRICHMENT_ENABLED))

    event_link_enrichment: Dict[str, Any] = {}
    if not clarification_required and effective_scan_kind == "event" and event_link_enrichment_enabled:
        event_link_enrichment = await _enrich_event_scan_from_links(
            ocr_text=ocr_text,
            seed_url=extracted_event.get("website"),
        )
        candidate = event_link_enrichment.get("event_candidate") or {}
        if candidate:
            if not extracted_event.get("title") and candidate.get("title"):
                extracted_event["title"] = candidate.get("title")
            if not extracted_event.get("description") and candidate.get("description"):
                extracted_event["description"] = candidate.get("description")
            if not extracted_event.get("starts_at") and candidate.get("starts_at"):
                extracted_event["starts_at"] = candidate.get("starts_at")
            if not extracted_event.get("location") and candidate.get("location"):
                extracted_event["location"] = candidate.get("location")
            if not extracted_event.get("website") and candidate.get("source_url"):
                extracted_event["website"] = candidate.get("source_url")
        discovered_links = event_link_enrichment.get("discovered_links") or []
        if discovered_links and not extracted_event.get("website"):
            extracted_event["website"] = discovered_links[0]
        if extracted_events:
            extracted_events[0] = extracted_event
        _scan_audit(
            "scan.event_link_enrichment.completed",
            {
                "visited_urls": event_link_enrichment.get("visited_urls") or [],
                "discovered_links_count": len(discovered_links),
                "event_candidate_found": bool(candidate),
            },
        )

    extracted = extracted_person
    if effective_scan_kind == "organization":
        extracted = {
            "name": extracted_org.get("name"),
            "title": None,
            "company": extracted_org.get("name"),
            "email": None,
            "phone": None,
            "website": extracted_org.get("website"),
            "address": None,
            "raw_lines": extracted_org.get("raw_lines") or [],
        }
    elif effective_scan_kind == "event":
        starts_at = extracted_event.get("starts_at")
        starts_at_iso = starts_at.isoformat() if isinstance(starts_at, datetime) else None
        extracted = {
            "name": extracted_event.get("title"),
            "title": None,
            "company": None,
            "email": None,
            "phone": None,
            "website": extracted_event.get("website"),
            "address": extracted_event.get("location"),
            "raw_lines": extracted_event.get("raw_lines") or [],
            "starts_at": starts_at_iso,
        }

    extracted_email = str(extracted_person.get("email") or "").strip().lower()
    pidp_user_result: dict[str, Any] = {"created": False, "pidp_user_id": None}
    created_targets: List[Dict[str, Optional[str]]] = []
    created_target_type: Optional[str] = None
    created_target_id: Optional[str] = None
    created_target_slug: Optional[str] = None
    created_target_name: Optional[str] = None

    def _append_created_target(
        target_type: str,
        *,
        target_id: Optional[str] = None,
        target_slug: Optional[str] = None,
        target_name: Optional[str] = None,
        target_image_url: Optional[str] = None,
    ) -> None:
        normalized_type = str(target_type or "").strip().lower()
        normalized_id = str(target_id or "").strip() or None
        normalized_slug = str(target_slug or "").strip() or None
        normalized_name = str(target_name or "").strip() or None
        if not normalized_type:
            return
        dedupe_key = (normalized_type, normalized_id or normalized_slug or normalized_name or "")
        for existing in created_targets:
            existing_key = (
                str(existing.get("type") or "").strip().lower(),
                str(existing.get("id") or existing.get("slug") or existing.get("name") or "").strip(),
            )
            if existing_key == dedupe_key:
                return
        target_url: Optional[str] = None
        if normalized_type == "organization" and normalized_slug:
            target_url = f"/orgs/{normalized_slug}"
        elif normalized_type == "event" and normalized_slug:
            target_url = f"/events/{normalized_slug}"
        created_targets.append(
            {
                "type": normalized_type,
                "id": normalized_id,
                "slug": normalized_slug,
                "name": normalized_name,
                "url": target_url,
                "image_url": str(target_image_url or "").strip() or None,
            }
        )

    normalized_org_name = str(extracted_org.get("name") or "").strip()
    normalized_person_derived_org_name = str((person_derived_org or {}).get("name") or "").strip()
    normalized_event_title = str(extracted_event.get("title") or "").strip()
    has_event_shape = bool(
        extracted_events
        or normalized_event_title
        or extracted_event.get("starts_at")
        or str(extracted_event.get("website") or "").strip()
    )
    should_create_person = False
    should_create_org = False
    should_create_event = False
    if not clarification_required:
        if normalized_scan_kind == "auto":
            should_create_person = bool(extracted_email)
            should_create_org = bool(normalized_org_name or (should_create_person and normalized_person_derived_org_name))
            should_create_event = has_event_shape
        elif normalized_scan_kind == "person":
            should_create_person = True
            should_create_org = bool(normalized_person_derived_org_name)
        elif normalized_scan_kind == "organization":
            should_create_org = True
        elif normalized_scan_kind == "event":
            should_create_event = True

    if not clarification_required and normalized_scan_kind == "auto" and not (
        should_create_person or should_create_org or should_create_event
    ):
        clarification_required = True
        processing_status = "clarification_required"
        clarification_message = (
            "No person, organization, or event fields were confidently detected. "
            "Please retry with a clearer image or choose scan type manually."
        )
        _scan_audit(
            "scan.classification.no_entities_detected",
            {
                "detected_scan_kind": detected_scan_kind,
                "classification_scores": classification_scores,
            },
        )

    created_org: Optional[Organization] = None
    if not clarification_required and should_create_org:
        org_payload = extracted_org
        if normalized_scan_kind == "person" and person_derived_org:
            org_payload = person_derived_org
        elif normalized_scan_kind == "auto" and should_create_person and person_derived_org:
            # In auto mode, prefer person-derived company for business-card patterns.
            org_payload = person_derived_org
        created_org = _create_or_find_organization_from_scan(
            session,
            actor_user_id=actor_user_id,
            extracted_org=org_payload,
            default_image_url=None,
        )
        created_org_image_url = _scan_created_target_image_url(
            submission_id,
            target_type="organization",
            target_id=str(created_org.id),
        )
        if created_org_image_url and (created_org.image_url or "").strip() != created_org_image_url:
            if not (created_org.image_url or "").strip():
                created_org.image_url = created_org_image_url
        _append_created_target(
            "organization",
            target_id=str(created_org.id),
            target_slug=created_org.slug,
            target_name=created_org.name,
            target_image_url=created_org.image_url,
        )

    if not clarification_required and should_create_event:
        event_candidates = extracted_events or [extracted_event]
        for event_candidate in event_candidates:
            event = _create_event_from_scan(
                session,
                actor_user_id=actor_user_id,
                extracted_event=event_candidate,
                default_image_url=None,
            )
            created_event_image_url = _scan_created_target_image_url(
                submission_id,
                target_type="event",
                target_id=str(event.id),
            )
            if created_event_image_url and not (event.image_url or "").strip():
                event.image_url = created_event_image_url
            if created_org and not event.host_org_id:
                event.host_type = EventHostType.ORG.value
                event.host_org_id = created_org.id
                event.host_user_id = None
            _append_created_target(
                "event",
                target_id=str(event.id),
                target_slug=event.slug,
                target_name=event.title,
                target_image_url=event.image_url,
            )

    if not clarification_required and should_create_person:
        if not extracted_email and normalized_scan_kind == "person":
            raise HTTPException(status_code=422, detail="Unable to detect email address from scanned person card")
        if extracted_email:
            pidp_user_result = await _create_or_find_pidp_user_from_business_card(
                email=extracted_email,
                full_name=extracted_person.get("name"),
            )
            _append_created_target(
                "person",
                target_id=str(pidp_user_result.get("pidp_user_id") or "").strip() or None,
                target_name=str(extracted_person.get("name") or extracted_email or "").strip() or None,
            )

    if not clarification_required and created_targets:
        created_targets = await _summarize_scan_targets_with_openai(
            ocr_text=ocr_text,
            created_targets=created_targets,
            audit_hook=_scan_ai_audit_hook,
        )

    if created_targets:
        primary = created_targets[0]
        created_target_type = str(primary.get("type") or "").strip() or None
        created_target_id = str(primary.get("id") or "").strip() or None
        created_target_slug = str(primary.get("slug") or "").strip() or None
        created_target_name = str(primary.get("name") or "").strip() or None

    submission = BusinessCardSubmission(
        id=submission_id,
        submitted_by_user_id=actor_user_id,
        submitted_by_email=current_user.get("email"),
        submitted_by_name=current_user.get("name"),
        image_filename=image.filename,
        image_content_type=content_type,
        image_size_bytes=len(image_bytes),
        image_sha256=image_sha256,
        image_storage_backend=image_storage_backend or None,
        image_storage_bucket=image_storage_bucket,
        image_storage_path=image_storage_path or None,
        image_storage_error=image_storage_error,
        ocr_provider=ocr_provider,
        ocr_text=ocr_text,
        extracted_name=extracted.get("name"),
        extracted_title=extracted.get("title"),
        extracted_company=extracted.get("company"),
        extracted_email=extracted_email or None,
        extracted_phone=extracted.get("phone"),
        extracted_website=extracted.get("website"),
        extracted_address=extracted.get("address"),
        extracted_metadata={
            "raw_lines": extracted.get("raw_lines") or [],
            "links": extracted_event.get("links") if isinstance(extracted_event.get("links"), list) else [],
            "events_detected": [
                {
                    "title": str(item.get("title") or "").strip() or None,
                    "starts_at": item.get("starts_at").isoformat() if isinstance(item.get("starts_at"), datetime) else None,
                    "location": str(item.get("location") or "").strip() or None,
                    "website": str(item.get("website") or "").strip() or None,
                }
                for item in extracted_events
            ],
            "scan_kind_requested": normalized_scan_kind,
            "scan_kind_detected": detected_scan_kind,
            "scan_kind": effective_scan_kind,
            "processing_status": processing_status,
            "clarification_required": clarification_required,
            "clarification_message": clarification_message,
            "classification_scores": classification_scores,
            "classification_top_confidence": top_confidence,
            "classification_confidence_margin": confidence_margin,
            "auto_min_confidence": auto_min_confidence,
            "auto_min_margin": auto_min_margin,
            "created_target_type": created_target_type,
            "created_target_id": created_target_id,
            "created_target_slug": created_target_slug,
            "created_target_name": created_target_name,
            "created_targets": created_targets,
            "event_starts_at": extracted.get("starts_at"),
            "event_link_enrichment_enabled": event_link_enrichment_enabled,
            "auto_clarification_enabled": auto_clarification_enabled,
            "event_link_enrichment": event_link_enrichment,
            "ai_audit_steps": ai_audit_steps,
        },
        notes=(notes or "").strip() or None,
        pidp_user_created=bool(pidp_user_result.get("created")),
        pidp_user_id=pidp_user_result.get("pidp_user_id"),
        notification_email_sent=False,
    )
    session.add(submission)
    _audit_event(
        session,
        actor=current_user,
        event_type="business_card.submitted",
        target_type="business_card_submission",
        target_id=str(submission.id),
        metadata={
            "scan_kind_requested": normalized_scan_kind,
            "scan_kind_detected": detected_scan_kind,
            "scan_kind": effective_scan_kind,
            "processing_status": processing_status,
            "clarification_required": clarification_required,
            "classification_top_confidence": top_confidence,
            "classification_confidence_margin": confidence_margin,
            "created_target_type": created_target_type,
            "created_target_id": created_target_id,
            "created_targets_count": len(created_targets),
            "extracted_email": submission.extracted_email,
            "pidp_user_created": submission.pidp_user_created,
            "ocr_provider": submission.ocr_provider,
            "event_link_enrichment_enabled": event_link_enrichment_enabled,
            "ai_call_count": len(ai_audit_steps),
        },
    )
    session.commit()
    session.refresh(submission)

    if not clarification_required and effective_scan_kind == "person" and extracted_email:
        background_tasks.add_task(
            _send_business_card_email_task,
            submission.id,
            extracted_email,
            submission.extracted_name,
            submission.submitted_by_name,
        )
    return submission


@router.get("/api/network/scans", response_model=List[BusinessCardSubmissionResponse])
@router.get("/api/network/business-cards", response_model=List[BusinessCardSubmissionResponse])
async def list_my_business_card_submissions(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
    scope: str = Query("mine", pattern="^(mine|public)$"),
):
    _require_authenticated_user(current_user)
    actor_user_id = _actor_user_id(current_user)
    if not actor_user_id and scope == "mine":
        return []
    safe_limit = max(1, min(limit, 1000))
    safe_offset = max(0, min(offset, 100000))
    query = session.query(BusinessCardSubmission)
    if scope == "mine":
        query = query.filter(BusinessCardSubmission.submitted_by_user_id == actor_user_id)
    else:
        # Shared "Completed Scans" feed excludes incomplete processing states.
        processing_status_expr = func.coalesce(
            BusinessCardSubmission.extracted_metadata["processing_status"].astext,
            "processed",
        )
        query = query.filter(processing_status_expr.in_(["processed", "clarification_required"]))
    rows = (
        query.order_by(BusinessCardSubmission.created_at.desc(), BusinessCardSubmission.id.desc())
        .offset(safe_offset)
        .limit(safe_limit)
        .all()
    )
    return rows


@router.get("/api/network/scans/{submission_id}/image")
@router.get("/api/network/business-cards/{submission_id}/image")
async def get_my_business_card_submission_image(
    submission_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_db),
):
    _require_authenticated_user(current_user)
    actor_user_id = _actor_user_id(current_user)
    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission:
        raise HTTPException(status_code=404, detail="Business card submission not found")
    can_view_shared_completed_scan = submission.processing_status in {"processed", "clarification_required"}
    if (
        submission.submitted_by_user_id != actor_user_id
        and not _can_use_sysadmin_override(current_user, ["org:admin.read", "org:*"])
        and not can_view_shared_completed_scan
    ):
        raise HTTPException(status_code=403, detail="Not authorized to view this scan image")
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


@router.get("/api/network/scans/{submission_id}/image/public/{target_type}/{target_id}")
async def get_public_scan_image_for_created_target(
    submission_id: uuid.UUID,
    target_type: str,
    target_id: str,
    session: Session = Depends(get_db),
):
    normalized_type = str(target_type or "").strip().lower()
    normalized_target_id = str(target_id or "").strip()
    if normalized_type not in {"organization", "event"} or not normalized_target_id:
        raise HTTPException(status_code=404, detail="Scan image not found")

    submission = (
        session.query(BusinessCardSubmission)
        .filter(BusinessCardSubmission.id == submission_id)
        .first()
    )
    if not submission or not submission.image_storage_path:
        raise HTTPException(status_code=404, detail="Scan image not found")

    allowed = False
    for target in submission.created_targets:
        if not isinstance(target, dict):
            continue
        if str(target.get("type") or "").strip().lower() != normalized_type:
            continue
        if str(target.get("id") or "").strip() != normalized_target_id:
            continue
        allowed = True
        break
    if not allowed:
        raise HTTPException(status_code=404, detail="Scan image not found")

    image_bytes = _load_business_card_image_bytes(
        storage_backend=(submission.image_storage_backend or "local"),
        storage_bucket=submission.image_storage_bucket,
        storage_path=submission.image_storage_path,
    )
    return Response(
        content=image_bytes,
        media_type=submission.image_content_type or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )
