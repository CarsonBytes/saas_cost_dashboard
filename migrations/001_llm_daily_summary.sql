-- Per-day usage summary for the shared llm_calls ledger, kept current by the
-- database itself (a trigger on every insert) instead of by readers.
-- Run once in the Supabase SQL editor (same project as llm_calls:
-- ajdxbxsxzbkromkrdkfq). New table + function + trigger only -- nothing
-- existing is read, modified, or backfilled.
--
-- WHY (2026-09-12): every "what's today's shared spend" status check across
-- this ecosystem (event-radar's fetch_shared_usage_today(), this dashboard's
-- own alert check, quant/study's equivalents) pulled EVERY raw row created
-- today and summed them client-side in Python -- a query whose cost grows
-- with the day's row count, paid on every single poll. Found live: event-
-- radar's public demo instance polled this every 10s from any open browser
-- tab, ~7,400 calls/day from that one code path alone, a real contributor to
-- the project's reported Supabase egress. Widening that poll interval
-- (10s -> 300s) cut the frequency; this fixes the actual shape of the
-- query -- O(1) bytes-on-the-wire regardless of how many calls happened
-- today, not just a less-frequent O(n) one. Callers switch from fetching
-- raw rows to reading this one small row: see event-radar's
-- backend/app/llm_logging.py::fetch_shared_usage_today() for the reference
-- reader (falls back to the old raw-row method if this table doesn't exist
-- yet, so rolling this out is not a breaking, ordered dependency).
--
-- Project attribution mirrors ledger.py::_project_of() / event-radar's own
-- llm_logging.py::_project_of() exactly: prefer the real `project` column
-- when a writer populates it (study, regtech), else fall back to the
-- `purpose` prefix convention ("quant:"/"events:", unprefixed = study) --
-- same precedence, not reinvented.

CREATE TABLE IF NOT EXISTS llm_daily_summary (
    day DATE PRIMARY KEY,              -- HKT calendar day
    total_calls INT NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC NOT NULL DEFAULT 0,
    calls_by_project JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION bump_llm_daily_summary() RETURNS TRIGGER AS $$
DECLARE
    d DATE := (NEW.created_at AT TIME ZONE 'Asia/Hong_Kong')::DATE;
    proj TEXT := COALESCE(
        NULLIF(NEW.project, ''),
        CASE
            WHEN NEW.purpose LIKE 'quant:%' THEN 'quant'
            WHEN NEW.purpose LIKE 'events:%' THEN 'events'
            ELSE 'study'
        END
    );
BEGIN
    INSERT INTO llm_daily_summary (day, total_calls, total_cost_usd, calls_by_project, updated_at)
    VALUES (d, 1, COALESCE(NEW.cost_usd, 0), jsonb_build_object(proj, 1), NOW())
    ON CONFLICT (day) DO UPDATE SET
        total_calls = llm_daily_summary.total_calls + 1,
        total_cost_usd = llm_daily_summary.total_cost_usd + COALESCE(NEW.cost_usd, 0),
        calls_by_project = llm_daily_summary.calls_by_project
            || jsonb_build_object(proj, COALESCE((llm_daily_summary.calls_by_project ->> proj)::INT, 0) + 1),
        updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- DROP + CREATE rather than CREATE OR REPLACE -- Postgres has no
-- "CREATE TRIGGER IF NOT EXISTS", so this is what makes re-running the
-- migration safe.
DROP TRIGGER IF EXISTS llm_calls_summary_trigger ON llm_calls;
CREATE TRIGGER llm_calls_summary_trigger
    AFTER INSERT ON llm_calls
    FOR EACH ROW EXECUTE FUNCTION bump_llm_daily_summary();

-- Backfill: this table only starts counting from the moment the trigger is
-- created above. One-time seed from existing history so today's (and any
-- already-elapsed HKT day's) totals aren't reported as zero the moment this
-- ships. Safe to re-run: ON CONFLICT overwrites each day with the same,
-- correctly-recomputed totals rather than double-counting.
WITH tagged AS (
    SELECT
        (created_at AT TIME ZONE 'Asia/Hong_Kong')::DATE AS day,
        COALESCE(
            NULLIF(project, ''),
            CASE
                WHEN purpose LIKE 'quant:%' THEN 'quant'
                WHEN purpose LIKE 'events:%' THEN 'events'
                ELSE 'study'
            END
        ) AS proj,
        COALESCE(cost_usd, 0) AS cost_usd
    FROM llm_calls
),
per_day_project AS (
    -- one row per (day, project): the per-project split each day needs
    SELECT day, proj, COUNT(*) AS calls, SUM(cost_usd) AS cost_usd
    FROM tagged
    GROUP BY day, proj
)
INSERT INTO llm_daily_summary (day, total_calls, total_cost_usd, calls_by_project, updated_at)
SELECT
    day,
    SUM(calls) AS total_calls,
    SUM(cost_usd) AS total_cost_usd,
    jsonb_object_agg(proj, calls) AS calls_by_project,
    NOW() AS updated_at
FROM per_day_project
GROUP BY day
ON CONFLICT (day) DO UPDATE SET
    total_calls = EXCLUDED.total_calls,
    total_cost_usd = EXCLUDED.total_cost_usd,
    calls_by_project = EXCLUDED.calls_by_project,
    updated_at = EXCLUDED.updated_at;
