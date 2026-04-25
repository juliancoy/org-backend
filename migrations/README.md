# org-backend migrations

This folder contains SQL migration artifacts for production rollout.

## 2026_04_24_network.sql
Adds LinkedIn-style network primitives:
- `organizations`
- `organization_memberships`
- `user_contact_pages`
- `organization_claim_requests`
- `network_events`
- `network_audit_events`

Apply this migration before deploying org-backend changes in strict environments where `Base.metadata.create_all` is not used for schema management.
