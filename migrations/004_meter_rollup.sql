-- Cross-project Supabase self-meter rollup (one row per app x endpoint x
-- minute). Run in the Supabase SQL editor. Idempotent.
--
-- WHY (2026-09-13): each app (dashboard, quant, event-radar, study) counts
-- its own Supabase REST calls in-process and flushes a local JSONL file --
-- but the dashboard can't reach the other projects' files, so per-app
-- attribution stayed fragmented. Writers also POST each minute-batch here
-- (~4 rows/min/project -- negligible against the tens of thousands of reads
-- it attributes), and the dashboard's Supabase tab reads this ONE table for
-- the cross-project picture. Same access posture as 002: RLS on, no
-- permissive policies (service_role readers bypass RLS).
--
-- Retention: one row per endpoint per minute per app is ~6K rows/day -- cap
-- it so this attribution table never becomes its own storage story. Adjust
-- the interval to taste; the dashboard tab only ever reads the trailing 24h.

CREATE TABLE IF NOT EXISTS public.meter_rollup (
    ts TIMESTAMPTZ NOT NULL,
    app TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    requests INT NOT NULL DEFAULT 0,
    bytes BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_meter_rollup_ts_app
    ON public.meter_rollup (ts DESC, app);

ALTER TABLE public.meter_rollup ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.meter_rollup FROM anon, authenticated;

-- No CREATE POLICY by design (see 002): RLS on + zero policies denies every
-- non-bypass role; service_role (all writers/readers) bypasses.

-- Keep 7 days; runs daily. pg_cron may not be enabled on every project, so
-- this is guarded: no extension, no schedule, no error (delete manually once
-- in a while in that case -- even a full year is only ~2M small rows).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'meter-rollup-retention') THEN
            PERFORM cron.unschedule('meter-rollup-retention');
        END IF;
        PERFORM cron.schedule('meter-rollup-retention', '0 3 * * *',
            $retention$DELETE FROM public.meter_rollup WHERE ts < NOW() - INTERVAL '7 days'$retention$);
    END IF;
EXCEPTION WHEN undefined_function OR undefined_table THEN
    -- cron schema present but job table missing (half-enabled extension):
    -- same deal, skip quietly.
    NULL;
END
$$;
