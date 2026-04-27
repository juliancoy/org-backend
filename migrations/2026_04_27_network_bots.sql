CREATE TABLE IF NOT EXISTS network_bots (
  id UUID PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE,
  full_name VARCHAR(255),
  pidp_user_id VARCHAR(255) NOT NULL UNIQUE,
  description TEXT,
  tags JSONB,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_user_id VARCHAR(255),
  updated_by_user_id VARCHAR(255),
  last_token_issued_at TIMESTAMPTZ,
  last_token_scope VARCHAR(64),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_network_bots_email ON network_bots(email);
CREATE INDEX IF NOT EXISTS idx_network_bots_pidp_user_id ON network_bots(pidp_user_id);
CREATE INDEX IF NOT EXISTS idx_network_bots_active ON network_bots(active);
CREATE INDEX IF NOT EXISTS idx_network_bots_created_by_user_id ON network_bots(created_by_user_id);
CREATE INDEX IF NOT EXISTS idx_network_bots_updated_by_user_id ON network_bots(updated_by_user_id);
