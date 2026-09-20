-- 005_meter_rollup_detail.sql -- self-identifying fingerprint for unlabeled
-- meter traffic (spec 2026-09-20). Writers only send `detail` once this
-- column exists (probe-and-remember in supabase_meter.py), and readers
-- tolerate its absence (ledger.py), so this is rollout-order-independent.
-- Run in the Supabase SQL editor. Idempotent.

alter table public.meter_rollup
    add column if not exists detail jsonb;

-- No index: detail is only ever read for the rare app='unknown' rows
-- (dashboard's Unlabeled-traffic card), never filtered at scale.
