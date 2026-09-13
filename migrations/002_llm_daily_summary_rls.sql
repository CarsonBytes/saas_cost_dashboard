-- Lock down the llm_daily_summary table created by 001_llm_daily_summary.sql.
-- Run in the Supabase SQL editor (same project as llm_calls). Idempotent.
--
-- WHY (2026-09-13): 001 created the table with no Row Level Security, so any
-- API key scoped to the project (including a leaked anon key) could read the
-- whole spend history. All first-party readers (this dashboard, quant,
-- event-radar, study) use the SERVICE_ROLE key, which bypasses RLS entirely
-- -- so enabling RLS with NO permissive policies changes nothing for them
-- while denying anon/authenticated roles outright. The trigger keeps working:
-- triggers execute as the table owner, unaffected by RLS (deliberately NOT
-- using FORCE ROW LEVEL SECURITY, which would apply policies to owners too).
--
-- Belt-and-braces REVOKEs below are redundant while RLS has no policies, and
-- that's the point: two independent layers must both fail before this table
-- leaks, instead of one forgotten GRANT being the whole story.

ALTER TABLE IF EXISTS public.llm_daily_summary ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.llm_daily_summary FROM anon, authenticated;

-- No CREATE POLICY statements by design: with RLS enabled and zero policies,
-- every non-bypass role is denied. service_role (all our readers) bypasses.
-- If a future reader needs anon/authenticated access, add an explicit,
-- reviewed policy then -- not a silent default now.
