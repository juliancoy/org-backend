from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    from org import db, logger

    try:
        # Check database
        async with db.async_pool.acquire() as conn:
            await conn.execute("SELECT 1")

        # Check Redis
        db.redis_client.ping()

        return {
            "status": "healthy",
            "database": "connected",
            "redis": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail="Health check failed")


@router.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Democratic Economic System API",
        "version": "2.0.0",
        "description": "A comprehensive democratic economic system with UBI, stock market, insurance, and fiscal policy",
        "documentation": "/docs",
        "health": "/health",
    }
