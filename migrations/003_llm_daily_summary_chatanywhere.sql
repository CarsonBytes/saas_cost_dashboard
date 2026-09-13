-- Track chatanywhere-quota calls in llm_daily_summary (created by
-- 001_llm_daily_summary.sql). Run in the Supabase SQL editor AFTER 001 (and
-- 002 for RLS, order between 002/003 doesn't matter). Idempotent.
--
-- WHY (2026-09-13): study's dashboard shows chatanywhere_calls against the
-- 200/day free-tier cap, which needs per-row PROVIDER info the summary table
-- didn't carry -- so study was the last reader still doing the O(n) raw-row
-- fetch this table exists to replace. These two columns let study read the
-- same one-row summary as quant/event-radar.
--
-- The is_chatanywhere rule mirrors study's core/llm.py reader EXACTLY (same
-- precedence, not reinvented): an explicitly-set provider is authoritative
-- (== 'chatanywhere'); otherwise the project heuristic (quant/events rows are
-- always via the shared proxy key) plus study's own embed calls
-- (purpose 'study:embed[%]'). Empty-string provider counts as unset, same as
-- the reader's falsy check.

ALTER TABLE IF EXISTS public.llm_daily_summary
    ADD COLUMN IF NOT EXISTS chatanywhere_calls INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS chatanywhere_by_project JSONB NOT NULL DEFAULT '{}';

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
    prov TEXT := NULLIF(NEW.provider, '');
    is_ca BOOLEAN := (prov = 'chatanywhere')
        OR (prov IS NULL AND (proj IN ('quant', 'events') OR NEW.purpose LIKE 'study:embed%'));
BEGIN
    INSERT INTO llm_daily_summary (day, total_calls, total_cost_usd, calls_by_project,
                                   chatanywhere_calls, chatanywhere_by_project, updated_at)
    VALUES (d, 1, COALESCE(NEW.cost_usd, 0), jsonb_build_object(proj, 1),
            CASE WHEN is_ca THEN 1 ELSE 0 END,
            CASE WHEN is_ca THEN jsonb_build_object(proj, 1) ELSE '{}'::jsonb END,
            NOW())
    ON CONFLICT (day) DO UPDATE SET
        total_calls = llm_daily_summary.total_calls + 1,
        total_cost_usd = llm_daily_summary.total_cost_usd + COALESCE(NEW.cost_usd, 0),
        calls_by_project = llm_daily_summary.calls_by_project
            || jsonb_build_object(proj, COALESCE((llm_daily_summary.calls_by_project ->> proj)::INT, 0) + 1),
        chatanywhere_calls = llm_daily_summary.chatanywhere_calls + CASE WHEN is_ca THEN 1 ELSE 0 END,
        chatanywhere_by_project = CASE WHEN is_ca THEN llm_daily_summary.chatanywhere_by_project
            || jsonb_build_object(proj, COALESCE((llm_daily_summary.chatanywhere_by_project ->> proj)::INT, 0) + 1)
            ELSE llm_daily_summary.chatanywhere_by_project END,
        updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Recompute every day from raw history (same idempotent overwrite pattern as
-- 001's backfill): days counted between 001 and 003 have zeros in the new
-- columns, and this fixes them in one pass. Safe to re-run.
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
        COALESCE(cost_usd, 0) AS cost_usd,
        CASE WHEN COALESCE(NULLIF(provider, ''), '') = 'chatanywhere' THEN 1
             WHEN (provider IS NULL OR provider = '')
              AND (COALESCE(
                       NULLIF(project, ''),
                       CASE
                           WHEN purpose LIKE 'quant:%' THEN 'quant'
                           WHEN purpose LIKE 'events:%' THEN 'events'
                           ELSE 'study'
                       END) IN ('quant', 'events')
                   OR purpose LIKE 'study:embed%') THEN 1
             ELSE 0 END AS is_ca
    FROM llm_calls
),
per_day_project AS (
    SELECT day, proj,
           COUNT(*) AS calls,
           SUM(cost_usd) AS cost_usd,
           SUM(is_ca)::INT AS ca_calls
    FROM tagged
    GROUP BY day, proj
),
per_day AS (
    SELECT day, SUM(calls)::INT AS total_calls, SUM(cost_usd) AS total_cost_usd,
           SUM(ca_calls)::INT AS chatanywhere_calls
    FROM per_day_project
    GROUP BY day
),
proj_agg AS (
    SELECT day, jsonb_object_agg(proj, calls) AS calls_by_project
    FROM per_day_project
    GROUP BY day
),
ca_agg AS (
    -- days with zero chatanywhere calls have no row here; the LEFT JOIN
    -- below COALESCEs them to '{}' rather than dropping the day.
    SELECT day, jsonb_object_agg(proj, ca_calls) AS chatanywhere_by_project
    FROM per_day_project
    WHERE ca_calls > 0
    GROUP BY day
)
INSERT INTO llm_daily_summary (day, total_calls, total_cost_usd, calls_by_project,
                               chatanywhere_calls, chatanywhere_by_project, updated_at)
SELECT d.day, d.total_calls, d.total_cost_usd, p.calls_by_project,
       d.chatanywhere_calls, COALESCE(c.chatanywhere_by_project, '{}'::jsonb), NOW()
FROM per_day d
JOIN proj_agg p USING (day)
LEFT JOIN ca_agg c USING (day)
ON CONFLICT (day) DO UPDATE SET
    total_calls = EXCLUDED.total_calls,
    total_cost_usd = EXCLUDED.total_cost_usd,
    calls_by_project = EXCLUDED.calls_by_project,
    chatanywhere_calls = EXCLUDED.chatanywhere_calls,
    chatanywhere_by_project = COALESCE(EXCLUDED.chatanywhere_by_project, '{}'::jsonb),
    updated_at = EXCLUDED.updated_at;
