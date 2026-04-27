from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


def _base_url() -> str:
    return (
        os.getenv("ORG_BASE_URL", "").strip()
        or os.getenv("ORG_BACKEND_URL", "").strip()
        or "http://127.0.0.1:8001"
    ).rstrip("/")


def _pat(required: bool = False) -> str:
    token = (
        os.getenv("ORG_PAT", "").strip()
        or os.getenv("ORG_MCP_PAT", "").strip()
        or os.getenv("PIDP_PAT", "").strip()
    )
    if required and not token:
        raise ValueError("ORG_PAT (or ORG_MCP_PAT / PIDP_PAT) is required for this tool")
    return token


def _headers(require_auth: bool = False) -> dict[str, str]:
    token = _pat(required=require_auth)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    require_auth: bool = False,
) -> dict[str, Any]:
    url = f"{_base_url()}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method.upper(),
            url,
            params=params,
            json=json_body,
            headers=_headers(require_auth=require_auth),
        )

    payload: Any
    try:
        payload = response.json()
    except Exception:
        payload = response.text

    return {
        "ok": response.is_success,
        "status": response.status_code,
        "url": str(response.request.url),
        "data": payload,
    }


mcp = FastMCP("org-backend")


@mcp.tool()
async def health() -> dict[str, Any]:
    """Check org backend health."""
    return await _request("GET", "/health")


@mcp.tool()
async def backend_root() -> dict[str, Any]:
    """Get org backend root payload."""
    return await _request("GET", "/")


@mcp.tool()
async def admin_me() -> dict[str, Any]:
    """Get current authenticated user admin state from /admin/me."""
    return await _request("GET", "/admin/me", require_auth=True)


@mcp.tool()
async def list_public_organizations(
    q: str = "",
    sort: str = "popular",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List public organizations from /api/network/orgs/public."""
    return await _request(
        "GET",
        "/api/network/orgs/public",
        params={"q": q, "sort": sort, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def get_public_organization(slug: str) -> dict[str, Any]:
    """Get a public organization by slug from /api/network/orgs/public/{slug}."""
    return await _request("GET", f"/api/network/orgs/public/{slug}")


@mcp.tool()
async def list_public_events(
    q: str = "",
    upcoming_only: bool = True,
    limit: int = 60,
    offset: int = 0,
) -> dict[str, Any]:
    """List public network events from /api/network/events/public."""
    return await _request(
        "GET",
        "/api/network/events/public",
        params={"q": q, "upcoming_only": upcoming_only, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def list_public_users(
    q: str = "",
    sort: str = "popular",
    limit: int = 60,
    offset: int = 0,
) -> dict[str, Any]:
    """List public user profiles from /api/network/users/public."""
    return await _request(
        "GET",
        "/api/network/users/public",
        params={"q": q, "sort": sort, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def list_organizations(
    q: str = "",
    mine: bool = False,
    only_unclaimed: bool = False,
    limit: int = 120,
    offset: int = 0,
) -> dict[str, Any]:
    """List authenticated org records from /api/network/orgs."""
    return await _request(
        "GET",
        "/api/network/orgs",
        params={
            "q": q,
            "mine": mine,
            "only_unclaimed": only_unclaimed,
            "limit": limit,
            "offset": offset,
        },
        require_auth=True,
    )


@mcp.tool()
async def create_organization(
    name: str,
    description: str | None = None,
    source_url: str | None = None,
    image_url: str | None = None,
    tags: list[str] | None = None,
    claim_on_create: bool = True,
) -> dict[str, Any]:
    """Create an organization via /api/network/orgs (authenticated)."""
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "source_url": source_url,
        "image_url": image_url,
        "tags": tags,
        "claim_on_create": claim_on_create,
    }
    return await _request(
        "POST",
        "/api/network/orgs",
        json_body=body,
        require_auth=True,
    )


@mcp.tool()
async def list_governance_motions(
    search: str = "",
    statuses: list[str] | None = None,
    motion_type: str | None = None,
    parent_motion_id: str | None = None,
) -> dict[str, Any]:
    """List governance motions from /api/governance/motions."""
    params: dict[str, Any] = {"search": search}
    if statuses:
        params["status"] = statuses
    if motion_type:
        params["type"] = motion_type
    if parent_motion_id:
        params["parent_motion_id"] = parent_motion_id
    return await _request("GET", "/api/governance/motions", params=params)


@mcp.tool()
async def create_governance_motion(
    title: str,
    body: str,
    proposer_type: str = "user",
    proposer_org_id: str | None = None,
    motion_type: str = "main",
    parent_motion_id: str | None = None,
    quorum_required: int = 5,
    proposed_body_diff: str | None = None,
) -> dict[str, Any]:
    """Create a governance motion via /api/governance/motions (authenticated)."""
    payload: dict[str, Any] = {
        "type": motion_type,
        "parent_motion_id": parent_motion_id,
        "title": title,
        "body": body,
        "proposed_body_diff": proposed_body_diff,
        "proposer_type": proposer_type,
        "proposer_org_id": proposer_org_id,
        "quorum_required": quorum_required,
    }
    return await _request(
        "POST",
        "/api/governance/motions",
        json_body=payload,
        require_auth=True,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
