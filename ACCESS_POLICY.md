# Org Backend Access Policy

## Scope
This policy defines authorization classes and access rules for the Org backend service.

## Access Classes
- `Public`: Any person, authenticated or unauthenticated, without elevated platform or org role.
- `Attendee`: A person who has attended a Code Collective event within the last 3 months.
- `Member`: A person who is an Organizer or participates in a Team.
- `Org Admin`: User with admin rights for a specific organization only.
- `SysAdmin`: Platform-level administrator with global elevated access.

Class precedence (highest to lowest):
`SysAdmin` > `Org Admin` > `Member` > `Attendee` > `Public`

## Role Definitions

### SysAdmin (platform-wide)
A user is treated as `SysAdmin` when one of the following is true:
- PIdP identity response (`/auth/me` or `/service/token-info.owner`) carries `is_sysadmin = true`.
- Break-glass fallback: their PIdP user ID is in `ORG_SYSADMIN_USER_IDS`.
- Break-glass fallback: SpiceDB permission check grants `org:{ORG_SYSADMIN_RESOURCE_ID}#db_admin`.

### Org Admin (organization-scoped)
A user is treated as `Org Admin` for a specific organization when one of the following is true:
- They are the claiming owner (`claimed_by_user_id`) of that organization.
- They have an `organization_memberships` row for that org with `role = "admin"`.
- They are `SysAdmin` (global override).

### Member (participation-scoped)
Constitution-aligned intent:
- Member status is tied to Organizer participation or Team participation.

Current backend implementation status:
- No dedicated persisted `member` role table/flag is enforced globally yet.
- Membership-like behavior currently exists for organizations via `organization_memberships`, but that is org-scoped and not equivalent to constitutional `Member`.

### Attendee (participation-scoped)
Constitution-aligned intent:
- Attendee status represents recent event attendance (within 3 months).

Current backend implementation status:
- No dedicated attendee role/flag is currently enforced as an authorization class.
- Event attendance data can be introduced later to drive this class.

### Public (baseline)
Constitution-aligned intent:
- Public includes all people outside elevated governance classes.

Current backend implementation status:
- Maps to users without `SysAdmin` and without applicable `Org Admin`.
- Includes unauthenticated users for explicitly public endpoints.

## Authorization Sources
- Identity/authentication: PIdP bearer token (`/auth/me` validation).
- Platform authorization authority: PIdP `is_sysadmin` claim.
- Platform break-glass fallback: SpiceDB + env override (`ORG_SYSADMIN_*`).
- Organization authorization: Organization ownership + membership tables.

## API Conventions
- `/admin/me` returns:
  - `is_sysadmin`
- Platform-protected endpoints must check `SysAdmin` explicitly.
- Org-scoped endpoints must check `Org Admin` for the relevant organization.
- Member/Attendee/Public are policy classes and should not be inferred from platform-admin flags alone.

## Dissolution Procedure (Constitution-Aligned)
Constitution source (`Title IX: Dissolution`) requires:
- a `three-fourths majority vote of all participating voters`, and
- a decision on the fate of remaining assets in the same vote.

Backend enforcement:
- Dissolution motions use governance `type = "dissolution"`.
- Creation of a dissolution motion requires asset disposition fields:
  - `dissolution_asset_disposition`
  - `dissolution_asset_recipient_name`
  - `dissolution_asset_recipient_type` (`non_profit` or `other_legal_entity`)
- Resolution uses a stricter threshold:
  - quorum must still be met (`participating_voters >= quorum_required`)
  - `yea >= ceil(0.75 * participating_voters)`
- Dissolution execution is a separate audited action:
  - `POST /api/governance/motions/{motion_id}/execute-dissolution`
  - requires `SysAdmin`
  - requires motion status `passed`

## Expected Enforcement Pattern
- Platform actions:
  - Require `is_sysadmin == true`.
  - Examples: global moderation queues, global settings, platform audit streams.
- Organization actions:
  - Require org-specific admin check.
  - Examples: org profile updates, org member management, org merge/control actions.
- Member actions (planned):
  - Require constitutional member eligibility once membership signals are implemented.
  - Examples: member-only governance workflows, private planning actions.
- Attendee actions (planned):
  - Require recent attendance signal once attendance tracking is implemented.
  - Examples: attendee-only signup or participation limits.
- Public actions:
  - Allowed only on endpoints explicitly marked public.

## Security Principles
- Least privilege: prefer organization-scoped authorization over platform-wide authorization.
- Explicit class separation: `SysAdmin` and `Org Admin` are distinct and should not be conflated.
- Deny by default: any missing/failed auth or role check must return `401/403`.

## Enforcement Model (Current Best Practice)
- Use a hybrid model:
  - `RBAC` for actor class checks (`SysAdmin`, `Org Admin`, `Member`, `Attendee`, `Public`).
  - `ABAC` for token attributes (`token_kind`, PAT `scope`, PAT `scope_grants`) and request/resource context.
- For sensitive SysAdmin APIs:
  - JWT session tokens are accepted for interactive admins.
  - PAT callers must satisfy both:
    - actor role check (`is_sysadmin == true`)
    - grant check (for example `org:admin.read` for reads, `org:admin.write` for mutations, or `org:*`).
- This prevents over-privileged PAT use even when a token owner is a platform admin.

## ReBAC Positioning
- `ReBAC` should be added where permissions are relationship-heavy and contextual (for example `viewer -> org -> team -> resource`), especially for organization/team/resource sharing.
- Existing SpiceDB integration remains the right substrate for this next phase.

## Operational Guidance
- Bootstrap a platform admin by setting:
  - `ORG_SYSADMIN_USER_IDS="<pidp_user_id>[,<pidp_user_id>...]"`
- After environment updates, recreate/restart backend containers so new values are applied.

## Constitution Alignment Notes
- Source reviewed: `https://codecollective.us/constitution.html` (draft, not ratified).
- The Constitution defines personnel classes including Public, Attendees, and Members.
- This backend policy now reflects those classes conceptually while preserving current enforceable classes (`SysAdmin`, `Org Admin`) until additional role signals are implemented.
