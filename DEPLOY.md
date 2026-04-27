# Org Backend Deployment Guide

This document defines best-practice deployment flows for `org` in **dev** and **prod** modes.

## Runtime Roles

- API containers run with `ORG_RUNTIME_ROLE=api`.
- Background jobs run in a dedicated worker container with `ORG_RUNTIME_ROLE=worker`.
- Optional `all` keeps legacy single-process behavior (API + jobs in one process).

## Dev Deployment

Use this flow when iterating locally and validating code changes.

### 1. Update code and dependencies
- Change `org/*.py` and/or `org/requirements.txt`.
- Keep MCP/FastAPI wiring in `org/org.py` so MCP remains in-process.

### 2. Rebuild dev image when needed
- `org/run.py` only builds the configured dev image tag if it does not exist.
- If dependencies changed, use a new dev image tag or remove the old image.

Example:
```bash
docker image rm org-backend-dev || true
```

### 3. Restart via orchestrator (preferred)
```bash
python3 run.py
```

### 4. Verify containers and health
```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'org|org-dev|org-worker|orgdb|org-redis'
curl -fsS http://localhost:8001/health
```

### 4a. SysAdmin bootstrap env (durable)
Set platform break-glass admins in `org/.env.org` so restarts preserve access:
```bash
cat > org/.env.org <<'EOF'
ORG_SYSADMIN_USER_IDS=89c36769-8bb3-4c6b-a1d9-5b26ae41b0d7
EOF
```
Notes:
- `run.py` auto-loads `org/.env.org` before container startup.
- Keep `ORG_SYSADMIN_USER_IDS` minimal (break-glass only).
- Primary platform SysAdmin authority should be managed in PIdP (`PIDP_ADMIN_EMAILS` / `PIDP_ADMIN_USER_IDS`) and consumed as `is_sysadmin` claim by Org.
- Re-run `python3 run.py` after any change to this file.

### 4b. Matrix chat bootstrap env (if OrgPortal chat should auto-sign-in)
Set these before `python3 run.py`:
```bash
export ORG_MATRIX_HOMESERVER_URL=http://synapse:8008
export ORG_MATRIX_SERVER_NAME=matrix.arkavo.org
export ORG_MATRIX_ADMIN_TOKEN=<synapse_admin_access_token>
export ORG_MATRIX_PASSWORD_SECRET=<long-random-secret>
export ORG_ALLOWED_PAT_SCOPES=org_portal,org_mcp,org_admin
```
Or place them in `org/.env.matrix` (preferred for local dev); `run.py` auto-loads that file.
Notes:
- `ORG_MATRIX_ADMIN_TOKEN` is only used server-side by the org backend.
- `ORG_MATRIX_PASSWORD_SECRET` should be stable across restarts so users keep seamless chat login.
- `ORG_ALLOWED_PAT_SCOPES` controls which PIdP PAT scopes can call Org backend APIs.

### 4c. Business Card Intake + Notification env
If you want scan intake (`/api/network/scans`, legacy alias `/api/network/business-cards`) enabled:
```bash
export ORG_BUSINESS_CARD_OCR_PROVIDER=openai
export ORG_OPENAI_API_KEY=<openai_api_key>
export ORG_BUSINESS_CARD_OCR_MODEL=gpt-4.1-mini
export ORG_BUSINESS_CARD_STORAGE_ENABLED=true
export ORG_BUSINESS_CARD_STORAGE_BACKEND=s3
export ORG_BUSINESS_CARD_S3_ENDPOINT_URL=http://minio:9000
export ORG_BUSINESS_CARD_S3_BUCKET=org-business-cards
export ORG_BUSINESS_CARD_S3_REGION=us-east-1
export ORG_BUSINESS_CARD_S3_ACCESS_KEY=minio
export ORG_BUSINESS_CARD_S3_SECRET_KEY=changeme
export ORG_BUSINESS_CARD_S3_USE_SSL=false
export ORG_BUSINESS_CARD_S3_PREFIX=business-cards
# Local fallback mode:
# export ORG_BUSINESS_CARD_STORAGE_BACKEND=local
# export ORG_BUSINESS_CARD_STORAGE_DIR=/var/lib/org/business-cards
# Optional: put RESEND_API_KEY in `./.env.resend` (repo root or `org/.env.resend`)
# and `run.py` will auto-map:
#   ORG_SMTP_RELAYHOST=smtp.resend.com
#   ORG_SMTP_RELAYHOST_PORT=587
#   ORG_SMTP_RELAY_USERNAME=resend
#   ORG_SMTP_RELAY_PASSWORD=$RESEND_API_KEY

# Local SMTP relay sidecar (recommended)
export ORG_ENABLE_SMTP_RELAY=true
export ORG_SMTP_RELAYHOST=<provider_smtp_host>      # e.g. email-smtp.us-east-1.amazonaws.com
export ORG_SMTP_RELAYHOST_PORT=587
export ORG_SMTP_RELAY_USERNAME=<provider_smtp_user>
export ORG_SMTP_RELAY_PASSWORD=<provider_smtp_password>
export ORG_SMTP_ALLOWED_SENDER_DOMAINS=arkavo.org

# App -> relay settings (defaults are usually correct)
export ORG_SMTP_HOST=org-smtp-relay
export ORG_SMTP_PORT=587
export ORG_SMTP_STARTTLS=false
export ORG_SMTP_USERNAME=
export ORG_SMTP_PASSWORD=
export ORG_SMTP_FROM=noreply@arkavo.org
export ORG_PORTAL_BASE_URL=https://dev.portal.arkavo.org
```
Notes:
- Endpoint validates image type/size before OCR.
- Uploaded business-card images are stored in MinIO (S3 API) by default for future reference.
- If SMTP is not configured, notification status is recorded as failed.
- For direct external SMTP (no relay sidecar), set `ORG_ENABLE_SMTP_RELAY=false` and configure `ORG_SMTP_*` directly.

### 5. Verify MCP mount
```bash
docker logs <prefix>org-dev --tail=200 | grep -i mcp
docker exec -it <prefix>org-dev python -c "import mcp; print('mcp ok')"
```

Replace `<prefix>` with your deployment distinguisher prefix (often empty).

## Prod Deployment

Use this flow for deterministic, reproducible updates.

### 1. Pin a production image
Use a commit-tagged image whenever possible:
```bash
export ORG_PROD_IMAGE=ghcr.io/juliancoy/org-backend:sha-<commit>
```

### 2. Configure launcher behavior
Recommended flags:
```bash
export ORG_SKIP_PROD_PULL=false
export ORG_DEV_IMAGE=org-backend-dev
```

If registry pull is unavailable and a local fallback is acceptable:
```bash
export ORG_SKIP_PROD_PULL=true
export ORG_ALLOW_LOCAL_PROD_BUILD=true
```

### 3. Deploy/redeploy
```bash
python3 run.py
```

### 4. Post-deploy verification
```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E '^org$|^org-worker$|^orgdb$|^org-redis$'
docker run --rm --network arkavo curlimages/curl:8.10.1 -fsS http://org:8001/health
```

### 5. Check logs for startup correctness
```bash
docker logs org --tail=200
docker logs org-worker --tail=200
```

## Recommended Discipline

- Treat `requirements.txt` changes as image changes (rebuild required).
- Keep DB volumes unless reset is intentional.
- Prefer `python3 run.py` over manual container starts to avoid config drift.
- Keep migrations in `org/migrations/` and apply them before prod rollouts that depend on new schema.

## Recovery

Restart non-DB services:
```bash
./stopAll.sh
python3 run.py
```

Full reset including DB volumes:
```bash
./stopAll.sh --db
python3 run.py
```
