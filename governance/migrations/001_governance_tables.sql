-- Governance tables for the Command Deck Compliance Radar.
-- Run these in the Supabase SQL editor (same project as the llm_calls ledger:
-- ajdxbxsxzbkromkrdkfq). New tables only -- nothing existing is modified.
-- The dashboard's Governance tab stays in a "run the SQL" banner state until
-- both tables exist (PostgREST cannot create tables, so this is manual).

CREATE TABLE IF NOT EXISTS governance_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name TEXT NOT NULL,
    condition_json JSONB NOT NULL, -- e.g., {"impact_level": ["high"], "affected_domains": ["HR", "Finance"]}
    action_type TEXT NOT NULL,     -- 'ALERT', 'REQUIRE_SIGN_OFF'
    enforcement_deadline TIMESTAMPTZ,
    status TEXT DEFAULT 'PENDING', -- 'PENDING', 'COMPLIED', 'OVERDUE', 'WAIVED'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS governance_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID REFERENCES governance_rules(id),
    action_taken TEXT NOT NULL,    -- 'AUTO_ALERT_SENT', 'MANUAL_OVERRIDE', 'STATUS_CHANGED'
    actor TEXT DEFAULT 'system',   -- 'system' or user email
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
