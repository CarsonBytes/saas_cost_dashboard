-- 006_llm_calls_token_truth.sql -- S1/S2 of the 2026-09-30 token-usage spec:
-- record an INDEPENDENT token estimate next to whatever the provider claims,
-- and flag rows whose claimed usage cannot be true.
--
-- WHY (measured 2026-09-30, events rerank, both bursts on the same model /
-- same code / same batch size):
--   * burst A (ASCII-only demo events): 16,157 reported tokens over 42,952
--     payload chars  -> 0.34 tok/char, i.e. a sane tokenizer for JSON English.
--   * burst B (47% CJK main events): 90,671 reported tokens over 68,504
--     payload chars -> 1.33 tok/char, up to 2.7 tokens per CJK character --
--     no real tokenizer does that. Re-scored with the deepseek-v4-pro rates
--     measured directly against api.deepseek.com on 2026-09-30 (6.29 chars
--     per ASCII token, 5.09 chars per CJK token), burst B's prompt is ~15.7K
--     tokens: the provider overstates CJK prompts ~5.8x.
--   * four rows claimed prompt_tokens = exactly 1,000,000 with latency_ms =
--     200 (a real 1M-token embedding call takes 800-2,900ms). Those four
--     phantom rows alone were 4.0M of the 4.02M "embedding tokens/14d".
--
-- prompt_tokens_est: computed by the WRITER from the prompt text it actually
-- sent (CJK chars / 4.8 + ASCII chars / 6.0, ceil) -- conservative (rounding
-- down the measured chars/token) and independent of the proxy. NULL when the
-- writer didn't have the prompt text (legacy writers, heartbeats).
--
-- suspect: true when the provider-reported number is physically impossible --
-- prompt_tokens > 200,000 in one call, or a token throughput over 50,000
-- tokens/second (the 1M-in-200ms rows are 5,000,000 tok/s). Rows are NEVER
-- deleted or zeroed -- the provider's own number stays in prompt_tokens
-- because that is the number the proxy enforces quota against; rollups that
-- want the truth read prompt_tokens_est and skip suspect rows.
--
-- Idempotent. Safe to run before the writers start populating the columns
-- (readers COALESCE).
alter table llm_calls
    add column if not exists prompt_tokens_est integer;

alter table llm_calls
    add column if not exists suspect boolean not null default false;

comment on column llm_calls.prompt_tokens_est is
    'Writer-computed CJK-aware estimate of prompt tokens (independent of the provider''s report). NULL when the writer had no prompt text.';
comment on column llm_calls.suspect is
    'Provider-reported usage is physically impossible (size/throughput sanity check); exclude from token rollups, keep for quota forensics.';

-- Backfill the one class of row we can prove is wrong without the prompt text:
-- the fabricated 1,000,000-token rows (and any other impossible throughput).
update llm_calls
   set suspect = true
 where prompt_tokens > 200000
    or (coalesce(latency_ms, 0) > 0
        and prompt_tokens::numeric / greatest(latency_ms, 1) > 50);
