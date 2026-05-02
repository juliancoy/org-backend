from fastapi import APIRouter, Depends, HTTPException

from org import (
    BusinessCardAbuseSettingsResponse,
    BusinessCardAbuseSettingsUpdate,
    _require_sysadmin,
    db,
    ensure_business_card_runtime_settings_table,
    get_business_card_runtime_settings,
    get_current_user,
    logger,
)

router = APIRouter()


@router.get("/api/admin/business-card/settings", response_model=BusinessCardAbuseSettingsResponse)
async def get_business_card_abuse_settings(
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.read", "org:*"],
        detail="SysAdmin access required",
    )
    return await get_business_card_runtime_settings()


@router.patch("/api/admin/business-card/settings", response_model=BusinessCardAbuseSettingsResponse)
async def update_business_card_abuse_settings(
    payload: BusinessCardAbuseSettingsUpdate,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.write", "org:*"],
        detail="SysAdmin access required",
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return await get_business_card_runtime_settings()

    allowed_content_types = updates.get("allowed_content_types")
    allowed_content_types_csv = ",".join(allowed_content_types) if allowed_content_types is not None else None
    try:
        await ensure_business_card_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE business_card_runtime_settings
                SET enabled = COALESCE($1, enabled),
                    per_user_limit_per_hour = COALESCE($2, per_user_limit_per_hour),
                    per_ip_limit_per_hour = COALESCE($3, per_ip_limit_per_hour),
                    global_limit_per_hour = COALESCE($4, global_limit_per_hour),
                    duplicate_hash_limit = COALESCE($5, duplicate_hash_limit),
                    duplicate_hash_window_seconds = COALESCE($6, duplicate_hash_window_seconds),
                    max_bytes = COALESCE($7, max_bytes),
                    allowed_content_types = COALESCE($8, allowed_content_types),
                    event_link_enrichment_enabled = COALESCE($9, event_link_enrichment_enabled),
                    auto_clarification_enabled = COALESCE($10, auto_clarification_enabled),
                    auto_min_confidence = COALESCE($11, auto_min_confidence),
                    auto_min_margin = COALESCE($12, auto_min_margin),
                    updated_at = NOW(),
                    updated_by = $13
                WHERE id = 1
                """,
                updates.get("enabled"),
                updates.get("per_user_limit_per_hour"),
                updates.get("per_ip_limit_per_hour"),
                updates.get("global_limit_per_hour"),
                updates.get("duplicate_hash_limit"),
                updates.get("duplicate_hash_window_seconds"),
                updates.get("max_bytes"),
                allowed_content_types_csv,
                updates.get("event_link_enrichment_enabled"),
                updates.get("auto_clarification_enabled"),
                updates.get("auto_min_confidence"),
                updates.get("auto_min_margin"),
                current_user.get("email"),
            )
    except Exception as exc:
        logger.error(f"Failed to update business card abuse settings: {exc}")
        raise HTTPException(status_code=503, detail="Business card settings service temporarily unavailable")
    return await get_business_card_runtime_settings()
