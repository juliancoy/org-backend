-- Org-network hardening migration
-- Date: 2026-04-24

CREATE TABLE IF NOT EXISTS organizations (
  id UUID PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  slug VARCHAR(255) NOT NULL UNIQUE,
  description TEXT,
  source_url TEXT UNIQUE,
  image_url TEXT,
  tags JSONB,
  seeded_from_events BOOLEAN NOT NULL DEFAULT FALSE,
  claimed_by_user_id VARCHAR(255),
  created_by_user_id VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_organizations_name ON organizations(name);
CREATE INDEX IF NOT EXISTS idx_organizations_slug ON organizations(slug);
CREATE INDEX IF NOT EXISTS idx_organizations_source_url ON organizations(source_url);

CREATE TABLE IF NOT EXISTS organization_memberships (
  id UUID PRIMARY KEY,
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id VARCHAR(255) NOT NULL,
  user_email VARCHAR(255),
  user_name VARCHAR(255),
  role VARCHAR(50) NOT NULL DEFAULT 'member',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_org_membership_org_user_unique
  ON organization_memberships(organization_id, user_id);
CREATE INDEX IF NOT EXISTS idx_org_memberships_org ON organization_memberships(organization_id);
CREATE INDEX IF NOT EXISTS idx_org_memberships_user ON organization_memberships(user_id);

CREATE TABLE IF NOT EXISTS user_contact_pages (
  id UUID PRIMARY KEY,
  user_id VARCHAR(255) NOT NULL UNIQUE,
  user_email VARCHAR(255),
  user_name VARCHAR(255),
  slug VARCHAR(255) NOT NULL UNIQUE,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  headline VARCHAR(255),
  bio TEXT,
  photo_url TEXT,
  email_public VARCHAR(255),
  phone_public VARCHAR(64),
  linkedin_url TEXT,
  website_url TEXT,
  links JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_contact_pages_slug ON user_contact_pages(slug);
CREATE INDEX IF NOT EXISTS idx_user_contact_pages_user_id ON user_contact_pages(user_id);

CREATE TABLE IF NOT EXISTS organization_claim_requests (
  id UUID PRIMARY KEY,
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  requested_by_user_id VARCHAR(255) NOT NULL,
  requested_by_email VARCHAR(255),
  requested_by_name VARCHAR(255),
  message TEXT,
  status VARCHAR(50) NOT NULL DEFAULT 'pending',
  reviewed_by_user_id VARCHAR(255),
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_org_claim_requests_org ON organization_claim_requests(organization_id);
CREATE INDEX IF NOT EXISTS idx_org_claim_requests_user ON organization_claim_requests(requested_by_user_id);
CREATE INDEX IF NOT EXISTS idx_org_claim_requests_status ON organization_claim_requests(status);

CREATE TABLE IF NOT EXISTS network_events (
  id UUID PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  slug VARCHAR(255) NOT NULL UNIQUE,
  description TEXT,
  starts_at TIMESTAMPTZ,
  ends_at TIMESTAMPTZ,
  location VARCHAR(255),
  source_url TEXT UNIQUE,
  ingest_key VARCHAR(255) UNIQUE,
  image_url TEXT,
  tags JSONB,
  host_type VARCHAR(20) NOT NULL DEFAULT 'unclaimed',
  host_user_id VARCHAR(255),
  host_org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
  claimed_by_user_id VARCHAR(255),
  created_by_user_id VARCHAR(255),
  seeded_from_events BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT check_network_event_host_type
    CHECK (host_type IN ('unclaimed', 'individual', 'org')),
  CONSTRAINT check_network_event_host_binding
    CHECK (
      (host_type = 'unclaimed' AND host_user_id IS NULL AND host_org_id IS NULL) OR
      (host_type = 'individual' AND host_user_id IS NOT NULL AND host_org_id IS NULL) OR
      (host_type = 'org' AND host_org_id IS NOT NULL AND host_user_id IS NULL)
    ),
  CONSTRAINT check_network_event_time_range
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)
);

CREATE INDEX IF NOT EXISTS idx_network_events_title ON network_events(title);
CREATE INDEX IF NOT EXISTS idx_network_events_slug ON network_events(slug);
CREATE INDEX IF NOT EXISTS idx_network_events_source_url ON network_events(source_url);
CREATE INDEX IF NOT EXISTS idx_network_events_ingest_key ON network_events(ingest_key);
CREATE INDEX IF NOT EXISTS idx_network_events_starts_at ON network_events(starts_at DESC);
CREATE INDEX IF NOT EXISTS idx_network_events_location ON network_events(location);
CREATE INDEX IF NOT EXISTS idx_network_events_host_type ON network_events(host_type);
CREATE INDEX IF NOT EXISTS idx_network_events_host_user_id ON network_events(host_user_id);
CREATE INDEX IF NOT EXISTS idx_network_events_host_org_id ON network_events(host_org_id);
CREATE INDEX IF NOT EXISTS idx_network_events_claimed_by ON network_events(claimed_by_user_id);

CREATE TABLE IF NOT EXISTS network_audit_events (
  id UUID PRIMARY KEY,
  actor_user_id VARCHAR(255),
  actor_email VARCHAR(255),
  event_type VARCHAR(100) NOT NULL,
  target_type VARCHAR(100) NOT NULL,
  target_id VARCHAR(255) NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_network_audit_events_created_at ON network_audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_network_audit_events_actor ON network_audit_events(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_network_audit_events_event_type ON network_audit_events(event_type);

ALTER TABLE network_events ADD COLUMN IF NOT EXISTS ingest_key VARCHAR(255);
CREATE UNIQUE INDEX IF NOT EXISTS idx_network_events_ingest_key_unique ON network_events(ingest_key) WHERE ingest_key IS NOT NULL;
