# Migration Ownership and Boundaries

## Purpose
Define clear ownership for schema changes so DDL does not drift between runtime code and migration artifacts.

## Ownership Model
- **Canonical schema changes**: `org/migrations/*.sql`
- **Runtime compatibility shims** in `org/org.py` are temporary safety nets only.
- **No permanent schema design in app startup code**.

## Rules
1. Every persistent schema change must ship as a numbered SQL migration file in `org/migrations/`.
2. Migration filenames use `YYYY_MM_DD_<domain>.sql`.
3. One migration should target one domain boundary where possible:
   - `network`
   - `governance`
   - `economy`
   - `auth`
   - `ingest`
4. App-level startup DDL (`ALTER TABLE IF EXISTS ...`) must be removed after rollout has converged in all environments.
5. Destructive operations (`DROP`, type narrowing, non-null backfills) require an explicit two-step plan:
   - additive migration
   - backfill/verification
   - cleanup migration

## Deployment Contract
- Apply SQL migrations before deploying app versions that require new columns/indexes.
- Keep migration ordering deterministic and idempotent where practical.
- If emergency runtime DDL is introduced, open a follow-up migration in the same PR or immediately next PR.

## Review Checklist
- Does the change include a migration file?
- Is rollback impact documented in the PR description?
- Are index/constraint names explicit and stable?
- Are long-running operations split to reduce lock risk?
