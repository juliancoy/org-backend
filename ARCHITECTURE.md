# Org Backend Architecture

## Scope
`org` is a FastAPI service that powers:
- Organization network and public directory (`/api/network/*`)
- Account and transaction APIs (`/api/accounts*`, `/api/transactions*`)
- Governance motions and voting (`/api/governance/*`)
- Economic simulation endpoints (UBI, stocks, insurance, fiscal, tax)
- Calendar ingest and periodic public feed sync
- MCP endpoints mounted in-process at `/mcp`

## Runtime Topology

### Process model
Single Python process running Uvicorn serves the FastAPI app (`org:app`) from `org/org.py`.

### Dependencies
- PostgreSQL (configured via `COCKROACH_DB_URL` and `COCKROACH_ASYNC_URL`)
- Redis (`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`)
- PIdP for identity (`PIDP_BASE_URL`, `PIDP_JWKS_URL`)
- SpiceDB for authorization (`SPICEDB_HTTP_URL`, `SPICEDB_PRESHARED_KEY`)

### Containers (dev/prod)
`org/run.py` orchestrates:
- `org` (prod image, no source mount)
- `org-dev` (local source mount + reload)
- `orgdb` (Postgres)
- `org-redis` (Redis)

## Application Structure

### Composition
- **Web app + models + API layer** live in `org/org.py`.
- **Domain modules** live under `org/domain/`:
  - `auth.py`
  - `network.py`
  - `ingest.py`
  - `governance.py`
  - `economy.py`
- **Launcher/orchestration** lives in `org/run.py`.
- **Dedicated worker entrypoint** lives in `org/worker.py`.
- **MCP server implementation** lives in `org/mcp_server.py` and is mounted into FastAPI when import succeeds.

### Lifespan boot sequence
On startup (`lifespan`):
1. Connect DB pools and Redis client.
2. Ensure SQLAlchemy tables exist.
3. Apply online ingest schema fixes.
4. Ensure UBI runtime settings table.
5. Seed organizations from event source file.
6. Bootstrap SpiceDB schema and admin relationships.
7. Start public calendar pull loop when enabled.

On shutdown:
1. Cancel calendar task.
2. Close DB and Redis connections.

## Authentication and Authorization

### Authentication
- `HTTPBearer(auto_error=False)` reads bearer token.
- `get_current_user` calls `PIdP /auth/me` to validate token and resolve identity.
- Missing/invalid credentials return `401`.
- User account is auto-provisioned in `accounts` on first valid login.

### Authorization
- Platform SysAdmin is sourced from PIdP identity claim (`is_sysadmin` in `/auth/me` and PAT token-info owner).
- Local break-glass fallback remains available via:
  - `ORG_SYSADMIN_USER_IDS` override, or
  - SpiceDB permission check (`org:portal#db_admin`).
- Organization admin is separate and scoped to an organization (`claimed_by_user_id`/membership role `admin`).
- Domain checks are enforced in endpoint handlers (org admin, motion management, etc.).
- SysAdmin account listing requires `is_sysadmin == true` and performs scoped PIdP lookups by configured sysadmin emails.
- Dissolution governance is modeled explicitly via:
  - `governance_motions.type = 'dissolution'`
  - `governance_dissolution_plans` (asset disposition payload and execution record)
  - `resolve` rule enforcing 3/4 participating-voter threshold.

## Data and Storage

### Primary datastore
PostgreSQL via SQLAlchemy models + asyncpg for selected async/background operations.

### Core entity groups
- **Economy**: accounts, transactions, UBI, stocks, orders, insurance, fiscal proposals, tax records.
- **Network**: organizations, memberships, events, claim requests, contact pages, audit events.
- **Governance**: motions, votes, comments, reactions.

### Cache/coordination
Redis is used for:
- API response caching (example: system metrics)
- Rate-limiting counters (`_throttle_action`)

## Ingest and Synchronization

### Ingest API
`POST /api/network/ingest/calendar`
- Protected by `ORG_INGEST_TOKEN` via header or bearer token.
- Uses constant-time token comparison.
- Upserts organizations and events by normalized source URLs and ingest keys.

### Periodic public pull
Background task (`_public_calendar_pull_loop`) periodically:
1. Fetches JSON event feeds from `ORG_PUBLIC_CALENDAR_FEEDS`.
2. Converts feed payload to internal ingest payload.
3. Reuses ingest upsert logic for consistency.

## Public Surface and Validation
- Public listing/profile endpoints are read-only and do not require auth.
- Public URLs are validated for scheme + host and block localhost/private IP targets.
- Contact updates validate/normalize public email and phone fields.
- Dissolution endpoints are authenticated and audited:
  - `GET /api/governance/motions/{id}/dissolution-plan`
  - `POST /api/governance/motions/{id}/execute-dissolution` (SysAdmin only)

## MCP Integration
- MCP is served by the same FastAPI process.
- App attempts mount order:
  1. `streamable_http_app()` at `/mcp`
  2. fallback `sse_app()` at `/mcp`
- No sidecar is required for MCP in this service.

## Operational Guidance

### Health and readiness
- `/health` checks DB and Redis and returns non-sensitive failure details.

### Build and deploy
- Prefer `python3 run.py` from repo root to keep service wiring consistent.
- Prod images are expected from `ghcr.io/juliancoy/org-backend` and can be pinned with `ORG_PROD_IMAGE=...:sha-<commit>`.
- Dev image rebuild is needed when `requirements.txt` changes.

### Security posture (current)
- Fail-closed API authentication (`401` when missing token).
- Authorization checks at endpoint and domain-logic boundaries.
- Ingest shared-secret gate with constant-time compare.
- Reduced data exposure on health/admin query paths.

## Known Design Constraints
- Most business logic currently resides in one module (`org.py`), which increases coupling.
- Startup currently creates sample/demo data; production environments should gate this behavior behind explicit config.
- Background loops run in-process; horizontal scaling requires care to avoid duplicate job execution without leader election.

## Current Refactor State
1. Domain logic is split into `org/domain/*` modules and consumed by `org.py`.
2. Background jobs can run in a dedicated role via `org/worker.py` and `ORG_RUNTIME_ROLE`.
3. Runtime flags now gate behavior:
   - `ORG_RUNTIME_ROLE`
   - `ORG_ENABLE_BACKGROUND_JOBS`
   - `ORG_ENABLE_SAMPLE_DATA`
   - `ORG_WORKER_LOCK_ENABLED`
   - `ORG_WORKER_LOCK_SECONDS`
4. Migration ownership boundaries are documented in `org/migrations/OWNERSHIP.md`.
