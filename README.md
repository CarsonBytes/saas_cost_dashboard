# Personal SaaS Cost Dashboard

A personal FinOps + ops hub for a small portfolio of live AI products (a trading system, an event-discovery app, an exam-prep RAG platform, and a RegTech compliance tracker) that share a single quota-limited LLM key, plus at-a-glance links to a handful of other shipped AI-engineering projects. Turns a Supabase table nobody was looking at into cost visibility, alerting, and a status page for the whole personal stack.

**Live:** [dashboard.carsonng.com](https://dashboard.carsonng.com)

A genuinely-used personal tool, not a demo — the design choices throughout favor verifying before trusting existing code, removing a failure point instead of patching around it, and being honest about what a number actually means before putting it in front of anyone.

---

## Architecture

```mermaid
flowchart TD
    subgraph Writers["Live products that write here, never read"]
        Q["quant<br/>analyst/usage_log.py"]
        S["study platform<br/>core/llm.py"]
        E["event radar<br/>app/llm_logging.py"]
    end

    DB[(Supabase Postgres<br/>llm_calls)]

    subgraph Hub["This app — D:\llm-usage-dashboard"]
        L["ledger.py<br/>PostgREST fetch + aggregate<br/>purpose-prefix fallback"]
        A["alerts.py<br/>editable threshold + history + dedup"]
        SV["services.py<br/>8 agents, reachability probes"]
        UI["app.py — NiceGUI"]
    end

    TG[("Telegram")]
    OTHER["quant / events / study / portfolio /<br/>regtech radar / 3 more AI projects"]
    CF["Cloudflare Tunnel<br/>quant-dashboard (shared)"]
    PUB(["dashboard.carsonng.com — public"])

    Q --> DB
    S --> DB
    E --> DB
    DB --> L --> UI
    L --> A
    A -- "breach detected" --> TG
    SV -- "HEAD probes" --> OTHER
    SV --> UI
    UI --> CF --> PUB
```

**The loop that ties it together:** the writing products never know this dashboard exists → `ledger.py` reads the shared table and normalizes attribution across inconsistent write conventions → `alerts.py` watches the same data for a threshold breach (itself dashboard-editable) and pushes to Telegram without needing anyone to have the page open → `services.py` gives the same page a one-glance answer to "is everything actually up" for the rest of the personal stack, cost-tracked or not.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Data access | Direct PostgREST (`httpx` + Supabase service-role key) | Removed an Edge Function middle layer that had silently failed; at ~1,400 rows, aggregating in the same process is simpler and has one fewer thing to deploy |
| UI | NiceGUI + ECharts, dark-mode aware | Already the house style across the other products — one less framework to context-switch between |
| Alerting | Telegram Bot API, reusing existing bot/chat | No new notification channel to configure or forget about |
| Settings | Flat JSON files, gitignored | The alert threshold and dedup state need to survive a process restart, not a database — a file is the right amount of infrastructure for one number |
| Deployment | Cloudflare Tunnel (shared with other subdomains) | One tunnel, one watchdog, one thing to keep alive |

## Project structure

```
D:\llm-usage-dashboard/
  app.py          NiceGUI page: agents strip, alert banner, KPIs, charts, tables, CSV export
  ledger.py        llm_calls fetch + aggregation, purpose-prefix fallback, efficiency/latency
                    ranking, attribution-quality metric, insight generation
  alerts.py        Editable daily cost threshold (persisted), bounded alert history, Telegram push
  services.py      Agent registry (icons + links) + HTTP reachability probes
  .env.example     Required/optional environment variables
```

## Running locally

```bash
python -m venv .venv && .venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env                                  # fill in SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY
python app.py                                          # http://localhost:8095
```

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional — the alert banner still works without them, just no push notification.

## Changelog

Newest first. Each entry is what shipped plus the reasoning behind it — not just a diff summary.

### 2026-08-04 — HKT day boundaries, model-usage migration tracking
- **Every day-boundary calculation switched from UTC to HKT** — the "Today" range filter, the daily cost trend's per-day bucketing, and the alert's same-day dedup all used UTC midnight, the same bug already found and fixed twice elsewhere in this ecosystem (quant's and event-radar's shared-quota counters). A call made at 2am HKT was being attributed to the previous UTC calendar day everywhere. Fixed with the same fixed-UTC+8-offset helper convention already established in those other projects, ported rather than reinvented a third time.
- **New model-usage breakdown, with a specific migration callout.** A `project × call_type × model` table answers "which model does each call site actually use," and a filtered view surfaces exactly which call types still call `gpt-4o-mini` instead of `gpt-5-mini` — verified live, found 4 real stragglers.

### 2026-07-30 (later) — public GitHub links, "My Agents", AWS demo link
- **"My Services" renamed to "My Agents"** throughout the UI and this README — a naming/branding decision, applied consistently rather than left half-updated.
- **Public GitHub links added for every agent that has one.** Quant, Event Radar, and Study Platform now link to their real (public) repos, reusing the same canonical short-link redirects the portfolio site already uses for consistency. Two deliberate omissions: AI Regulation Radar has no git remote configured at all, and Portfolio's repo is kept private by choice — neither gets a link that would 404 for anyone else visiting.
- **Fixed a severe page-load hang**: the agents health check was running synchronously on every single request — up to 12 sequential HTTP probes at a 5s timeout each, confirmed live to hang the site for exactly 60 seconds before failing outright, on both the public URL and a direct localhost request (ruling out the tunnel as the cause). The existing background refresh loop was already doing this correctly; the redundant synchronous call in the page-render path was pure risk that only became visible once the agent list grew large enough for a slow link to matter. Removed the call, parallelized the probes with a thread pool — TTFB dropped from ~13s to a consistent ~1.2-1.6s.
- **Added a live demo link (a real merged PR) for AWS AI Code Review**, which previously only linked to its GitHub repo with nothing to actually look at.

### 2026-07-30 — rename, service link cleanup
- **Renamed to "Personal SaaS Cost Dashboard"**, in both the browser title and the on-page heading — the earlier "LLM Usage Dashboard" name undersold what it had actually grown into (a full ops hub, not just a cost log).
- **AI Regulation Radar's card now shows its public link as primary**, with the Access-gated instance as a secondary "Private" reference link — same two-link pattern already used for the trading system's paper/live split.

### 2026-07-29 — three more AI projects added, found by actual audit
- **Reviewed every local repo's real `git remote`** (not memory, which turned out to be accurate but incomplete) to confirm which projects were genuinely GitHub-hosted personal work versus client repos — deliberately excluded client projects from this hub by explicit scope decision.
- **Read the portfolio site's own source** (`assets/js/main.js`, the actual data feeding its project grid) for every AI-tagged entry not yet represented here, surfacing three real shipped projects: a human-in-the-loop AI risk-gate demo, an AI sprint-retrospective generator, and an automated AWS code-review/security-scanning gate. Verified both live demo URLs actually resolved before adding — one is a HuggingFace Space, one a Streamlit Cloud app whose sleep/wake-gate response still counts as "up" under the same tolerant status check already used for Access-gated agents.
- **Added a fifth-then-more agent card for AI Regulation Radar** and gave the dashboard itself a money-bag favicon.

### 2026-07-28 (latest) — latency, attribution, editable threshold, CSV, icons
- **Latency panel** surfaces `latency_ms`, which had been fetched since the very first rewrite but never actually shown anywhere. First real finding from it: one call type averages tens of seconds per call, by far the slowest in the whole ledger.
- **Cost-attribution-quality KPI** — turns the known "only one project tags itself properly" gap into a live, visible percentage instead of a fact that only lived in a code comment.
- **Alert threshold became a dashboard setting.** Previously a load-once environment variable; now editable from the page and persisted to a small settings file, verified with a full round trip through a real process restart.
- **CSV export** and a **$/call column** on the call-type breakdown table.
- **Agent icons** replaced the plain colored status dot — each agent gets a distinct Material icon that itself carries the up/down color, after evaluating two layout options and keeping the existing card layout rather than a bigger dock-style redesign.

### 2026-07-28 (later) — per-project trend, alert history, efficiency ranking, dark mode
- **Cost-per-day chart became a per-project stacked area**, replacing a single total line that couldn't say *which* project caused a given day's spike.
- **Alert history**: the alert state file grew from "remember the last alerted value" to a capped, append-only log, surfaced as its own table.
- **Efficiency leaderboard**: every model and provider ranked cheapest-first by $/1K tokens — immediately surfaced that the least-attributed project also happens to be the most expensive one, in the same table.
- **Dark-mode toggle** added; verified the layout at a 375px mobile width needs no fixes (Quasar's own table wrapper already scrolls internally, the header already wraps).

### 2026-07-28 (public ops hub)
- **Extended into a personal ops hub at `dashboard.carsonng.com`.** Added a "My Agents" strip linking out to the other products, each with a live reachability status dot. Exposed publicly by adding one ingress rule to the Cloudflare Tunnel already serving the others, rather than standing up a second tunnel.
- **Chose fully public over Access-gated**, matching the event-discovery app's exposure level rather than the trading dashboard's — this page shows relative cost trends, not anything that needs gating.

### 2026-07-28 — cost dashboard fix, alerting, insights
- **Diagnosed and fixed a dashboard that had never worked.** Traced a 404 on the Edge Function this app depended on back to a deploy that most likely never happened, then removed that dependency entirely rather than fixing the deploy — see above.
- **Added a daily cost-threshold alert with Telegram push**, reusing the trading system's existing bot/chat and message format. Verified the send actually lands, not just that the function runs without raising.
- **Added an auto-generated "top spender" insight line and a projected-monthly-cost KPI** — the actual point of a FinOps view (what should I look at first) rather than raw charts alone.
- Worked around a NiceGUI version constraint (`ui.page cannot be used ... when UI is defined in the global scope`) by moving the periodic background check to an `app.on_startup`-registered `asyncio` task instead of a bare `ui.timer`.

### 2026-07-16 — initial build
- First version: KPI cards, daily calls trend, by-project/by-provider/by-model/by-environment breakdowns, call-type table, date-range toggle — reading from the Edge Function later found to be non-functional.

## Roadmap

- Two of the projects writing to the shared ledger still don't populate the real `project`/`provider` columns (only the study platform does) — their spend shows as `(untagged)` in the by-provider breakdown and ranks worst on the efficiency leaderboard by construction. The fix belongs in their own write paths, not in this reader; flagged as a follow-up rather than worked around here.
- Configurable monthly budget (today's projection derives an implied monthly budget from the daily alert threshold × 30, a reasonable proxy but not an independently-set figure).
