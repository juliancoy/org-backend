# Constitution Implementation Roadmap

## Scope
This roadmap translates the Code Collective constitution draft into backend-deliverable work for the Org service.

Reference:
- https://codecollective.us/constitution.html (draft, unratified)

## Implementation Principles
- Keep `SysAdmin` and org-scoped `Admin` separate.
- Add constitutional classes (`Public`, `Attendee`, `Member`) as explicit, queryable state.
- Enforce policy in backend checks, not only UI.
- Prefer additive migrations and explicit API contracts.

## Phase 0: Baseline and Policy (Done/Current)
- `ACCESS_POLICY.md` created with class model and precedence.
- Platform `is_sysadmin` separated from org `admin`.
- `/admin/me` returns `is_sysadmin`.
- Dissolution workflow implemented per Title IX:
  - governance `type = dissolution`
  - mandatory asset disposition plan fields
  - 3/4 participating-voter threshold at resolve time
  - audited `execute-dissolution` action

## Phase 1: Role and Eligibility Substrate (High Priority)
Constitution mapping:
- Title II (Personnel), Title V (Teams)

### 1.1 New core tables
- `teams`
  - `id`, `name`, `slug`, `description`, `status`, timestamps
- `team_memberships`
  - `team_id`, `user_id`, `role`, `active`, timestamps
- `event_attendance`
  - `event_id`, `user_id`, `attended_at`, `source`, `verified_by`
- `user_role_overrides` (optional)
  - explicit manual overrides (`member`, `attendee`, `organizer`, etc.) with expiry

### 1.2 Derived eligibility service
- Add a single role resolution function:
  - input: `user_id`
  - output: `{is_public, is_attendee, is_member, is_org_admin, is_sysadmin, reasons[]}`
- `Attendee` rule: attendance within trailing 90 days.
- `Member` rule: organizer OR active team participation.

### 1.3 API additions
- `GET /api/authz/me` (or extend `/admin/me`) with full class snapshot.
- `GET /api/teams`, `POST /api/teams`, `POST /api/teams/{id}/members`.
- `POST /api/events/{id}/attendance`.

Acceptance:
- Backend can deterministically compute class membership for any authenticated user.

## Phase 2: Voting Procedure Compliance (High Priority)
Constitution mapping:
- Title IV (Voting)

### 2.1 Motion lifecycle constraints
- Require second before voting transition.
- Enforce minimum voting window (>= 24 hours).
- Enforce quorum rule: half of eligible Organizer voters (round down).

### 2.2 Tie-breaker
- Add Lead Organizer concept and tie-break resolution logic.
- Persist tie-break action and rationale in audit log.

### 2.3 Endpoint and domain updates
- Tighten transition checks in motion state machine.
- Add explicit validation errors for quorum/window/second requirements.

Acceptance:
- Motion outcomes cannot violate constitutional voting mechanics.

## Phase 3: Meetings and Attendance Enforcement (Medium-High)
Constitution mapping:
- Title III (Meetings)

### 3.1 Meeting model
- New `meetings` entity:
  - `type` (`meetup`, `retro_public`, `retro_private`, `review`, `special`)
  - visibility class requirements
  - scheduling + channel metadata

### 3.2 Access controls by meeting type
- Public Retro visibility rules.
- Private Retro/Review visibility for eligible classes only.
- Organizer call-special permissions.

### 3.3 Attendance integration
- Use meeting attendance to drive `Attendee` class recency.

Acceptance:
- Meeting visibility/editing/attendance are consistently enforced server-side.

## Phase 4: Justice Workflow (Medium)
Constitution mapping:
- Title VI (Justice)

### 4.1 Grievance pipeline
- `grievances` table with reporter, accused, category, narrative, status, SLA timestamps.
- intake endpoint and review endpoint.

### 4.2 Discipline actions
- `disciplinary_actions` table:
  - `nonaction`, `council`, `mediation`, `reprimand`, `expulsion`
  - start/end, expunge policy metadata

### 4.3 Persona Non Grata enforcement
- Block event/org interactions for active PNG records.
- Include override path for SysAdmin emergency actions.

Acceptance:
- Justice outcomes are auditable and enforceable in runtime authorization.

## Phase 5: Finance Governance Rules (Medium)
Constitution mapping:
- Title VII (Finance)

### 5.1 Reimbursement policy controls
- Policy table for max per-event reimbursable amount and allowed categories.
- Require vote artifact reference for non-preapproved purchases.

### 5.2 Fiscal calendar controls
- Add fiscal-year boundary validation for reporting and budget actions.

Acceptance:
- Finance endpoints enforce constitutional constraints, not just convention.

## Phase 6: Communications Governance (Medium-Low)
Constitution mapping:
- Title VIII (Communications)

### 6.1 Channel registry
- Canonical channels with allowed class write/read rules.

### 6.2 Outbound publishing controls
- Policy checks for who can publish external announcements by content type.

Acceptance:
- Communication privileges are explicit and enforceable.

## Phase 7: Constitutional Amendments and Ratification (Medium-Low)
Constitution mapping:
- Title IV amendment mechanics + document governance

### 7.1 Constitutional versioning
- Store constitution version, status (`draft`, `ratified`, `superseded`), effective dates.

### 7.2 Amendment workflow
- Dedicated amendment motion type with stricter threshold configuration.

Acceptance:
- Constitutional changes are versioned, auditable, and policy-driven.

## Phase 8: Dissolution Hardening (Medium)
Constitution mapping:
- Title IX (Dissolution)

### 8.1 Legal closure evidence
- Attach legal documentation artifacts to dissolution execution records.
- Validate recipient metadata against policy (non-profit/legal-entity requirements).

### 8.2 Financial closure controls
- Add explicit checklist state for liabilities and remaining asset transfer completion.
- Integrate with fiscal host handoff records where applicable.

### 8.3 Runtime closure mode
- Add optional system closure mode after passed dissolution (allow only closure-critical operations).

Acceptance:
- Dissolution execution produces an auditable legal/financial closure record aligned with constitutional requirements.

## Cross-Cutting Technical Work
- Add migration ownership and migration playbooks for each phase.
- Expand audit event coverage for all authorization-sensitive actions.
- Add integration tests for class resolution and endpoint guards.
- Add seed/test fixtures for roles, teams, attendance, and voting scenarios.

## Suggested Delivery Order
1. Phase 1 (role substrate)
2. Phase 2 (voting compliance)
3. Phase 3 (meetings/attendance)
4. Phase 4 (justice)
5. Phase 5 (finance)
6. Phase 6 (communications)
7. Phase 7 (constitutional versioning)

## Minimal Milestone Definition
Milestone A (ready for pilot):
- Phase 1 + Phase 2 complete
- `Attendee` and `Member` resolvable
- Voting constraints constitutionalized
- integration tests passing in CI
