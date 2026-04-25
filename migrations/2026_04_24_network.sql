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
