# Personal SaaS Cost Dashboard

A personal FinOps + ops hub for a small portfolio of live AI products (a trading system, an event-discovery app, an exam-prep RAG platform, and a RegTech compliance tracker) that share a single quota-limited LLM key, plus at-a-glance links to a handful of other shipped AI-engineering projects. Turns a Supabase table nobody was looking at into cost visibility, alerting, and a status page for the whole personal stack.

**Live:** [dashboard.carsonng.com](https://dashboard.carsonng.com)

Built as both a genuinely-used personal tool and a demonstration of the kind of judgment an AI PM/governance role actually needs: verify before trusting existing code, prefer removing a failure point over patching around it, and be honest about what a number actually means before putting it in front of anyone.

---

## What makes this more than a metrics page

- **Found a production dependency that had been silently dead for 12 days, before writing a single line of the fix.** This dashboard already existed — built once, then apparently never checked again. Its data path called a companion Supabase Edge Function; that function returned a plain HTTP 404. Rather than assume the plan ("wire up alerting on top of what's there") was still valid, the first step was `curl`-ing the endpoint directly to confirm it was actually broken, then tracing why (never deployed — no Supabase CLI on this machine, no deploy history). Building on unverified assumptions about "already working" code is exactly the failure mode that lets a broken system look fine for 12 days.
- **Chose to remove the failure point, not patch it.** The fix on offer was "just deploy the Edge Function." Instead, the Edge Function layer was dropped entirely — the dashboard now queries the shared `llm_calls` table directly over PostgREST and aggregates in Python. At this data volume (~1,400 rows), a second deployable added latency, an extra piece of infrastructure to keep working, and — as just proven — a silent single point of failure, for zero benefit over doing the same aggregation in the same process that already needed an HTTP client.
- **The headline cost number comes with an honest asterisk, not a false precision.** Every project's `cost_usd` is computed from a hardcoded reference price table applied to real token counts — not a real invoice. All products are routed through a free-tier proxy, so the actual amount billed is often zero. The dashboard treats this correctly: a **relative cost-trend signal** for comparing projects/models/providers, not a number to report as real spend. Getting this distinction right — and saying so — is the difference between a useful internal metric and a misleading one.
- **Attribution stays correct even though the data is inconsistent by design — and the dashboard measures its own blind spot.** Only one of the writing projects populates the `project`/`provider` columns that actually exist on the table; the others only ever wrote a `purpose` string like `"quant:board_scan"`. Every aggregation preserves the original prefix-parsing fallback so cost still attributes to the right project. A dedicated "cost attribution quality" KPI goes further — it shows the real percentage of spend that carries a genuine provider tag (currently a low single digit), turning a known gap into something visibly trackable instead of a fact buried in a code comment.
- **A per-project stacked trend, not just a total line.** The daily cost chart breaks spend down by project instead of showing one aggregate number — a single-line chart can tell you spend went up, not who caused it.
- **An efficiency leaderboard, not just a cost total.** Every model and provider seen in the window gets ranked cheapest-first by $/1K tokens. Real finding it surfaced immediately: the `(untagged)` bucket ranks *worst* on the provider table — the exact projects missing real attribution are also, unsurprisingly, the ones driving the bulk of spend.
- **Alerting was verified to actually reach a phone, not just verified to compile — and the threshold is a dashboard setting, not a deploy.** The Telegram push reuses the same bot/chat and message convention already proven in production (the trading system's own risk alerts), confirmed with a real one-off test message before being called done. The daily-cost threshold itself is editable from the page and persisted to disk, so tuning it no longer means editing an env var and restarting the server — verified with a real round trip (edit → save → survives a process restart → reflected in the KPI and chart).
- **A bounded alert history, not just a banner that disappears.** Every fired alert is recorded (capped, oldest dropped) and shown as its own table, closing the "did this actually fire before, and when" gap a pure real-time banner leaves open.
- **Background delivery works with zero browser tabs open.** A cost alert that only fires while someone happens to be looking at the dashboard isn't an alert. Ran into (and worked around) a real constraint in the installed NiceGUI version that rejects a bare global-scope timer — solved with `asyncio` tasks registered on app startup instead, so both the cost-alert check and the services health-check run on a schedule independent of any connected client.
- **The "My Services" status strip tells the truth about what "down" means.** Several linked services sit behind a Cloudflare Access login; a healthy instance still responds to a probe with a redirect to that login page, not a 200. The health check treats *any* response as "up" and only a connection failure as "down" — a naive `status_code == 200` check would falsely flag every gated-but-healthy service as broken.
- **The services list itself came from an actual audit, not a guess.** Rather than assume which projects belonged on the hub, every local repo's real `git remote` was checked against memory (catching a stale assumption), and the portfolio site's own source was read for every AI-tagged project not yet represented — surfacing three real shipped projects (a risk-gate demo, a sprint retrospective generator, an automated code-review gate) that would otherwise never have been listed here.
- **Reused existing infrastructure instead of building new.** Getting this onto the public internet at `dashboard.carsonng.com` took one new ingress line in an *already-running* Cloudflare Tunnel shared by other subdomains, plus one DNS route command — not a second tunnel, not a new deployment story to maintain.

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
        SV["services.py<br/>8 services, reachability probes"]
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
  app.py          NiceGUI page: services strip, alert banner, KPIs, charts, tables, CSV export
  ledger.py        llm_calls fetch + aggregation, purpose-prefix fallback, efficiency/latency
                    ranking, attribution-quality metric, insight generation
  alerts.py        Editable daily cost threshold (persisted), bounded alert history, Telegram push
  services.py      Service registry (icons + links) + HTTP reachability probes
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

### 2026-07-30 — rename, service link cleanup
- **Renamed to "Personal SaaS Cost Dashboard"**, in both the browser title and the on-page heading — the earlier "LLM Usage Dashboard" name undersold what it had actually grown into (a full ops hub, not just a cost log).
- **AI Regulation Radar's card now shows its public link as primary**, with the Access-gated instance as a secondary "Private" reference link — same two-link pattern already used for the trading system's paper/live split.

### 2026-07-29 — three more AI projects added, found by actual audit
- **Reviewed every local repo's real `git remote`** (not memory, which turned out to be accurate but incomplete) to confirm which projects were genuinely GitHub-hosted personal work versus client repos — deliberately excluded client projects from this hub by explicit scope decision.
- **Read the portfolio site's own source** (`assets/js/main.js`, the actual data feeding its project grid) for every AI-tagged entry not yet represented here, surfacing three real shipped projects: a human-in-the-loop AI risk-gate demo, an AI sprint-retrospective generator, and an automated AWS code-review/security-scanning gate. Verified both live demo URLs actually resolved before adding — one is a HuggingFace Space, one a Streamlit Cloud app whose sleep/wake-gate response still counts as "up" under the same tolerant status check already used for Access-gated services.
- **Added a fifth-then-more service card for AI Regulation Radar** and gave the dashboard itself a money-bag favicon.

### 2026-07-28 (latest) — latency, attribution, editable threshold, CSV, icons
- **Latency panel** surfaces `latency_ms`, which had been fetched since the very first rewrite but never actually shown anywhere. First real finding from it: one call type averages tens of seconds per call, by far the slowest in the whole ledger.
- **Cost-attribution-quality KPI** — turns the known "only one project tags itself properly" gap into a live, visible percentage instead of a fact that only lived in a code comment.
- **Alert threshold became a dashboard setting.** Previously a load-once environment variable; now editable from the page and persisted to a small settings file, verified with a full round trip through a real process restart.
- **CSV export** and a **$/call column** on the call-type breakdown table.
- **Service icons** replaced the plain colored status dot — each service gets a distinct Material icon that itself carries the up/down color, after evaluating two layout options and keeping the existing card layout rather than a bigger dock-style redesign.

### 2026-07-28 (later) — per-project trend, alert history, efficiency ranking, dark mode
- **Cost-per-day chart became a per-project stacked area**, replacing a single total line that couldn't say *which* project caused a given day's spike.
- **Alert history**: the alert state file grew from "remember the last alerted value" to a capped, append-only log, surfaced as its own table.
- **Efficiency leaderboard**: every model and provider ranked cheapest-first by $/1K tokens — immediately surfaced that the least-attributed project also happens to be the most expensive one, in the same table.
- **Dark-mode toggle** added; verified the layout at a 375px mobile width needs no fixes (Quasar's own table wrapper already scrolls internally, the header already wraps).

### 2026-07-28 (public ops hub)
- **Extended into a personal ops hub at `dashboard.carsonng.com`.** Added a "My Services" strip linking out to the other products, each with a live reachability status dot. Exposed publicly by adding one ingress rule to the Cloudflare Tunnel already serving the others, rather than standing up a second tunnel.
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
