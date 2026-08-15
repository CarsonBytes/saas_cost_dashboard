-- Phase 2: regulatory_updates consumer table + agent_slug linkage.
-- Run these in the Supabase SQL editor AFTER 001_governance_tables.sql.
-- Both statements are idempotent (IF NOT EXISTS / IF NOT EXISTS column).

-- 1. Task B source table: RegTech Radar's pipeline (next step) writes one row
--    per detected regulatory change; the dashboard's compliance engine polls
--    unconsumed rows and turns each into a PENDING governance_rules task.
--    You can also insert rows manually to test the engine (see README).
CREATE TABLE IF NOT EXISTS regulatory_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,              -- e.g. 'EU AI Act Digital Omnibus: Article 5 amended'
    affected_articles TEXT[] DEFAULT '{}',  -- e.g. {'Article 5', 'Article 6(1)'}
    deadline TIMESTAMPTZ,             -- compliance deadline, if known
    impact_hint TEXT,                 -- 'high' | 'medium' | 'low' (from assessment)
    source TEXT DEFAULT 'regtech',
    consumed BOOLEAN DEFAULT FALSE,   -- already turned into a governance_rules row
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Agent linkage for compliance_health (Task C): which agent a rule applies
--    to. Use the exact services.py display name as the slug, e.g.
--    'Quant Trading (Paper)', 'Event Radar', 'Study Platform'.
--    NULL = global/unspecified (counts into the tab's KPIs but flags no card).
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS agent_slug TEXT;
