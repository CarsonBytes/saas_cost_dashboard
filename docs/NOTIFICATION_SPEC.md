# Notification Policy v1 — critical only to Telegram

Status: accepted 2026-10-04
Applies to: `llm-usage-dashboard` (`alerts.py`, `noc.py`, `app.py`, `governance/`,
`scripts/infra-watchdog.sh`) and `quant` (`dashboard/core/notify.py`,
`scripts/gateway-push.sh`, `scripts/gateway-login-watchdog.sh`).

## 1. Why

Retro over 13 logs (~60.7k lines) in `/home/cap`, both deploy hooks and the
WSL crontab found that Telegram was being used as a *log sink*, not a paging
channel:

| Source | Measured | Rate |
|---|---|---|
| `infra-watchdog` restart notices | 2,488 sends, all 2026-09-22→09-26 (2,367 on 09-26 alone) | 0 since — fixed |
| gateway "relogin cycle started — approve 2FA" | 299 cycles in `gateway-restart.log` | ~4.3/day |
| NOC `alert sent` | 11 success / 0 failed, 2026-10-01→10-04 | ~3.7/day |
| NOC "auto-unlocked ✅" | 4 incidents in 3 days | ~1.3/day |
| quant (`TG-PUSH` mirror) | 0 in 7 days, both instances | 0 |
| cost threshold | never fired (no `alert_state.json`) | 0 |

Steady state ≈ **9 messages/day**, of which ~50% describe events that are
already resolved or were self-initiated.

Meanwhile actual health is fine: 1,395 failing checks out of 36,728 (3.80%)
over 8 days, and **90% of those failures are the two trading agents** (market
closed / IBKR session contention). Every other agent sits at 98.7–99.8%.
Restarts in state: 6 total. Active locks: 0. Auth locks: 0.

Bugs are *not* frequent. The volume came from pushing routine and
self-recovering events at the same priority as genuine failures.

Root cause: `alerts.send_telegram()` had **no severity parameter**, so all 17
call sites pushed identically, while `quant`'s `notify.py` already had a
`_PUSH_LEVELS` filter with no `critical` tier. Two different policies in one
chat, neither of them what the operator asked for.

## 2. Severity taxonomy (unified across all senders)

| Level | Meaning | Pushes? |
|---|---|---|
| `critical` | Broken **and needs a human within minutes**, or a security event | **Telegram + ntfy** |
| `error` | Broken, but auto-healing / no action needed now | no — banner + digest |
| `warning` | Degraded, threshold crossed, budget | no — banner + digest |
| `info` | Success, user-initiated, routine | no — log/changelog only |

**Decision rule:** *does ignoring this for 12 hours make it worse?*
If the answer is no, it is not `critical`.

## 3. Routing

| Channel | Contents |
|---|---|
| Telegram | `critical` only |
| Dashboard banner | `critical` + `error` + `warning` |
| Local log / changelog | everything |
| Daily digest, 08:00 HKT | counts of `error`/`warning` by agent + top 5 events. One message, non-urgent. Dashboard-only by default (`DIGEST_TELEGRAM=0` to force, `1` to allow Telegram). |

## 4. Call-site reclassification

| Site | Before | After | Rationale |
|---|---|---|---|
| `noc.py` agent locked | push | **critical** | auto-heal disabled on a live agent |
| `noc.py` passcode lockout | push | **critical** | security |
| `noc.py` auto-quarantine (policy) | push | **critical** | policy paused a container |
| `noc.py` `alert_only` down/stale | push | **error** | largest single source; self-heals or needs time |
| `noc.py` auto-unlocked ✅ | push | **info** | already resolved — nothing to do |
| `app.py` operator quarantine | push | **info** | the operator just clicked it |
| `governance` OVERDUE / status change / match | push | **warning** | slow-moving |
| `alerts.py` daily cost threshold | push | **warning** | budget, not an outage |
| `commands.py` command reply | push | **`force=True`** | a reply the operator is waiting on; bypasses the level filter only, still cooled down, capped and audited |
| `infra-watchdog` "… — restarting" | push | **error** | 2,488 in 5 days proved this floods |
| `infra-watchdog` `escalate_once` | n/a | **critical** | only when auto-heal gives up |
| `gateway-push` cycle started / approve 2FA | push every cycle | **critical, first cycle only** | recovery needs a human, but it fired on all 299 cycles |
| `gateway-push` N cycles FAILED | push | **critical** | manual action needed |
| `gateway-push` quiet-hours outage | push | **error** | expected during IBKR's weekly reset |
| `gateway-push` manual UI restart | push | **info** | the operator just clicked it |
| `notify.py` `_PUSH_LEVELS` | `{"warning","error"}` | **`{"critical"}`** | adds the missing `critical` tier |
| `ledger.py` risk flag (injection in a logged call) | push | **error** | real signal, but not minutes-scale; already has its own per-(service,kind) cooldown |
| quant `notable_events.record` → `notify.send` | `level="error"` | **critical** | fires only when a dedupe group *turns red* — a state transition, which is exactly the decision rule |

## 5. Anti-flood guarantees

1. **Hard daily cap: 10 criticals/day** (per calendar day, HKT). On exceeding,
   collapse the remainder into one `+N more (see dashboard)` message.
2. **Per-key cooldown 15 min** on `{source}:{key}`; **identical-text cooldown
   5 min** (quant's existing `_COOLDOWN_SEC` semantics, now shared).
3. **Per-incident, not per-cycle**, for anything driven by a retry loop — the
   gateway's 2FA prompt fires once per outage, not once per attempt.
4. **State-transition only**: never push `failed → failed`, only `ok → failed`
   and `failed → ok`.

## 6. Delivery is audited

Every attempt — sent, suppressed-by-level, suppressed-by-cooldown,
suppressed-by-cap, failed — appends one JSON object to:

```
D:\claude\alerts\audit\alerts.jsonl          (host path, written by WSL cron)
  ├─ container view:  /app/alerts_audit/alerts.jsonl   (bind mount)
  └─ host view:       /mnt/d/claude/alerts/audit/alerts.jsonl
```

```json
{"ts":"...","level":"critical","source":"noc","key":"Quant Trading (Paper)",
 "outcome":"sent","http_status":200,"digest_eligible":false,"text":"..."}
```

`outcome ∈ sent | failed | not-configured | suppressed-level |
suppressed-cooldown-key | suppressed-cooldown-text | suppressed-cap`.

Two details that each cost a real debugging session on 2026-10-04:

- **The compose mount must be absolute.** The deployed compose runs from
  `/home/cap/llm-usage-dashboard`, so the `../claude/alerts/audit` form
  resolves to `/home/cap/claude/alerts/audit` — a *different* directory from
  `/mnt/d/claude/alerts/audit` where the WSL-side cron writers append (they
  were separate inodes; the container saw an empty dir while the watchdog had
  already written four records). Absolute path, no `..`, no ambiguity.
- **`http_status` must be valid JSON.** `curl -w '%{http_code}'` reports `000`
  when it cannot connect at all, and raw interpolation produces
  `"http_status":000` — a leading-zero number, which JSON rejects. The line
  then fails to parse, silently dropping the one record that says a *critical*
  push failed. Both bash writers normalise `000`/empty to `null`.

The digest (§3) is **derived from this file**, not from a parallel counter —
that is what makes it a true accounting of what did NOT page, and what lets
the host-side bash producers (`infra-watchdog.sh`, `gateway-push.sh`) feed the
same rollup as the Python senders with no shared state to keep in sync.

**Quant's `notify.py` audit** is its pre-existing `notify: TG-PUSH [...]` log
line (`docker logs quant-dashboard-docker | grep TG-PUSH`), which has been the
documented outbound record since 2026-09-30. Unifying it into `alerts.jsonl`
needs `ALERT_AUDIT_DIR` mounted into that container — deferred, because the
feed already exists and is queryable today.

Nothing about alert delivery may live only in the sender's memory. An alert
that was sent and an alert that was dropped must be distinguishable after the
fact — that ambiguity cost a full debugging session on 2026-10-04.

## 7. Acceptance criteria

- Steady-state Telegram ≤ **3/day** (baseline 9); hard ceiling 10 + 1 collapse.
- Zero pushes with `level ∈ {info, warning, error}` outside the daily digest.
- A 20-hour storm (2026-09-26 scale) produces ≤ 3 messages, not 2,367.
- The 2FA prompt still arrives for every **new** incident.
- Every send attempt appears in `alerts.jsonl` with a non-empty `outcome`.
- Every line of `alerts.jsonl` parses as JSON (`json.loads` on all of it).
- The dashboard container and the WSL cron writers append to the *same* file
  (`docker exec <noc> cat /app/alerts_audit/alerts.jsonl` shows watchdog rows).
