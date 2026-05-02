from fastapi import APIRouter, Depends, HTTPException

from org import (
    UBIRuntimeSettingsResponse,
    UBIRuntimeSettingsUpdate,
    _require_sysadmin,
    db,
    ensure_ubi_runtime_settings_table,
    get_current_user,
    get_ubi_runtime_settings,
    logger,
)

router = APIRouter()


@router.get("/api/ubi/settings", response_model=UBIRuntimeSettingsResponse)
async def get_ubi_settings(
    current_user: dict = Depends(get_current_user),
):
    """Read runtime UBI settings used by the UBI worker."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return await get_ubi_runtime_settings()


@router.patch("/api/ubi/settings", response_model=UBIRuntimeSettingsResponse)
async def update_ubi_settings(
    payload: UBIRuntimeSettingsUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update runtime UBI settings used by the UBI worker."""
    if current_user.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_sysadmin(
        current_user,
        pat_required_grants=["org:admin.write", "org:*"],
        detail="SysAdmin access required",
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return await get_ubi_runtime_settings()

    interval_seconds = updates.get("interval_seconds")
    dena_annual = updates.get("dena_annual")
    dena_precision = updates.get("dena_precision")
    entity_types = updates.get("entity_types")
    entity_types_csv = ",".join(entity_types) if entity_types is not None else None

    try:
        await ensure_ubi_runtime_settings_table()
        async with db.async_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ubi_runtime_settings
                SET interval_seconds = COALESCE($1, interval_seconds),
                    dena_annual = COALESCE($2, dena_annual),
                    dena_precision = COALESCE($3, dena_precision),
                    entity_types = COALESCE($4, entity_types),
                    updated_at = NOW(),
                    updated_by = $5
                WHERE id = 1
                """,
                interval_seconds,
                float(dena_annual) if dena_annual is not None else None,
                dena_precision,
                entity_types_csv,
                current_user.get("email"),
            )
    except Exception as exc:
        logger.error(f"Failed to update UBI settings: {exc}")
        raise HTTPException(status_code=503, detail="UBI settings service temporarily unavailable")
    return await get_ubi_runtime_settings()
