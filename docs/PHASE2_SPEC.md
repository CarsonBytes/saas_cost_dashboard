# Phase 2 Spec — Command Deck

Status: **spec, not yet built.** Phase 1 (agent cards + Personal NOC health/restart engine) shipped 2026-08-14 and is running in production. This spec is updated with what Phase 1's live behavior actually showed, most importantly the Study Platform auto-restart thrash that a person only noticed by reading the incident log at the right moment.

## 1. Dependency topology — badge-first, never graph-first

- **Stay badge-first.** Three upstream dependencies and six monitored agents do not need a force-directed layout to be legible, and the existing "blocked by: Supabase" / "blocked by: LLM API" badge already covers the case that actually matters for a decision (is this agent's failure explained by something I can't fix by restarting it).
- **Optional secondary view only:** a simple static diagram showing which agents depend on Supabase versus the shared LLM API (writers: quant paper / event radar / study → Supabase + chatanywhere; portfolio / regtech → neither). Never the default view; nothing force-directed, nothing animated.

## 2. "Simulate crash" chaos button — needs a real target first

- The three cards that look like obvious safe-to-break demo candidates — Change Impact Assessor, Sprint Analyzer, AWS AI Code Review — are **not monitored at all**, so a simulated crash has nothing to demonstrate self-healing on.
- **Required before building:** either
  1. add one small, explicitly-nonproduction agent that is monitored and auto-heal-eligible specifically to serve as the chaos target (recommended — e.g. a throwaway container that writes a heartbeat to the ledger), or
  2. reuse Event Radar.
- **Do NOT use Study Platform for chaos demonstrations** until the 2026-08-15 `restart_on_staleness` fix has shipped and been observed stable — otherwise the demo is just reproducing a live bug instead of showcasing a feature.
- Button behavior once a target exists: mark the chosen agent unhealthy (simulate by stopping the container / pointing its probe at a dead URL), let the monitor observe the failure, and show the normal recovery path (restart, grace, healthy) live. Log a "chaos test" incident so the event is attributable.

## 3. Quota-wastage cost estimate — always explicitly labelled as an estimate

- Render as a value **prefixed with `~` and suffixed `(est.)`**, e.g. `~$1.23 (est.)` — never a bare dollar figure. This is the same convention the dashboard already uses wherever a cost is inferred rather than directly measured.
- Definition to settle at build time: the spend attributable to shared-key quota exhaustion (failed/retried calls against the chatanywhere daily quota), derived from the ledger's failure/retry signals.

## 4. NEW (from Phase 1 review): "restarting isn't healing" tripwire

- **The gap it closes:** the Study Platform thrash (7 restarts / 30 min, two lock→alert→unlock cycles) was caught by a person happening to read the incident log. Nothing in Phase 1 flagged "restarts keep happening and the agent never actually gets better" on its own.
- **What to build:** a per-agent counter of restarts that did not lead to recovery, distinct from the existing 3-per-hour lock (that one measures restart *frequency*; this one measures restart *effectiveness*).
- **Proposed semantics (decision needed at build time):** a restart "failed to heal" if the agent is not **fully healthy** (liveness ok AND readiness ok — not merely "reachable") at the end of the 5-minute warmup grace that follows it. Consecutive failed-to-heal restarts (proposal: **≥ 2**) surface a small warning — on the agent card and as an incident-log entry ("auto-heal not recovering") — and clear again once the agent next achieves full health.
- **Why the grace-boundary definition matters:** with the naive "no healthy check in between" reading, the warmup-grace checks (which count as healthy) would mask exactly the Study case this exists to catch. Defining recovery as "fully healthy after grace" catches both "restart doesn't fix liveness" and "restart doesn't fix staleness" (the Study class) for any agent and any future threshold mistake.
- **Relationship to the lock:** the tripwire is an earlier, softer signal than the lock (fires on 2 failed-to-heal restarts, well before 3 restarts lock the agent). It should be display-only this round — no automatic action beyond the warning, so it stays cheap and safe.

## 5. Explicitly out of scope for Phase 2

- Quant Trading (Live) remains untouched — no monitoring, no health checks, no restart logic. Its restart handling is a separate, carefully-tuned system this dashboard must not duplicate or race against. Revisit deliberately, not as a side effect.
- The health/restart engine's NYSE session logic, circuit breaker, cooldown/lock, and incident log are verified working (Phase 1 selftest + live incident log); no rework.
