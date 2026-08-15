"""Static registry for the other carsonng.com properties, rendered as the
agent-cards strip above the cost dashboard -- this app is a personal ops hub,
not just a cost viewer.

DELIBERATELY A PURE REGISTRY: no probing, no runtime state, no health logic.
All monitoring lives in noc.py (liveness probes, Supabase-ledger readiness,
blocked-by dependency checks, restart authority, cooldown/lock, incident log,
7-day uptime), which reads this registry. Keeping the card definitions a
one-glance data table and the health logic in one testable module is the
point of the split.

Per-entry fields:
  name / desc / icon / links  -- as rendered on the card.
  business_impact            -- manually-assigned string, "high" | "medium" |
                                 "low". Drives the Governance tab's
                                 High-Impact Watchlist and the risk ledger's
                                 scan scope: "low" agents are never scanned
                                 (counted only), "medium"/"high" are. Not
                                 editable from the UI -- keep it a registry
                                 field (ADDED 2026-08-15).
  monitor                     -- False = pure link card (no status dot, never
                                 probed at all). True = monitored (noc.py).
  restart                     -- "auto_heal": noc.py restarts this agent's own
                                 Docker container on failure (a 3-per-hour
                                 cooldown locks it until cleared from the UI).
                                 "alert_only": Telegram alert on failure, no
                                 restart ever. "none": nothing beyond the
                                 status dot.
  project_tag / freshness_sec -- readiness (freshness) config: which project
                                 tag in the shared Supabase llm_calls ledger to
                                 watch, and how old that tag's latest write may
                                 be before the agent reads as "degraded"
                                 (reachable but stale). None = liveness only.
  freshness_table             -- optional per-agent override of the freshness
                                 data source. Default (absent) is the shared
                                 `llm_calls` ledger filtered by project_tag --
                                 right for enforced-cadence agents. Study
                                 Platform overrides with "answer_log" (its own
                                 usage table), because practice-mode correct
                                 answers never write to the LLM ledger, so the
                                 LLM-ledger signal read "idle" right after the
                                 app was used (FIXED 2026-08-15).
  restart_on_staleness        -- whether a STALE readiness result may itself
                                 trigger an auto-restart. Only agents with a
                                 real, enforced write cadence (Quant Paper's
                                 scan loop, Event Radar's ingest schedule) get
                                 True: restarting can genuinely fix a broken
                                 loop there. Usage-driven agents (Study
                                 Platform) stay False -- staleness there just
                                 means "nobody has used it", which a container
                                 restart cannot fix, so it degrades the card
                                 but never restarts on its own. Default False.
  container                   -- Docker container name to restart (auto_heal).

Health semantics (details in noc.py): liveness is a plain reachability probe
(did we get *any* HTTP response, not a specific status code) -- these apps sit
behind Cloudflare Access, so a healthy instance still answers with a redirect
to the Access login page, not a 200. A connection error/timeout is the only
thing that means "down".
"""
from __future__ import annotations

SERVICES = [
    {
        "name": "Quant Trading (Paper)",
        "business_impact": "high",   # trading system -- a fault is money
        "desc": "Weekly-TSMOM dashboard, paper trading",
        "icon": "show_chart",
        "links": [("Paper", "https://quant.carsonng.com"),
                  ("GitHub", "https://carsonng.short.gy/quant-trade-analysis-github")],
        "monitor": True,
        "restart": "auto_heal",
        "project_tag": "quant",
        "freshness_sec": 900,          # writes every ~1min during market hours
        "restart_on_staleness": True,  # enforced scan loop -- a stalled loop is a real fault
        "container": "quant-dashboard-docker",
    },
    {
        # Splitting the old combined Quant Trading card: Live runs as a native
        # Windows deployment with its own separately-tuned watchdog, and this
        # dashboard must not monitor, probe, or restart it -- that's deliberate
        # (see the round's scope notes), not an oversight.
        "name": "Quant Trading (Live)",
        "business_impact": "high",   # live 17-ETF trading
        "desc": "Live 17-ETF trading, native deployment with own watchdog",
        "icon": "show_chart",
        # Access-gated like the other Private links, so the card labels it
        # Private (the generic renderer adds the lock icon off the label).
        "links": [("Private", "https://quant-live.carsonng.com")],
        "monitor": False,
        "restart": "none",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
    {
        "name": "Event Radar",
        "business_impact": "medium", # public-facing app
        "desc": "AI event-discovery portfolio app, real HK events",
        "icon": "event",
        "links": [("Demo", "https://events-demo.carsonng.com"),
                  ("Private", "https://events.carsonng.com"),
                  ("GitHub", "https://carsonng.short.gy/event-radar-github")],
        "monitor": True,
        "restart": "auto_heal",
        "project_tag": "events",
        "freshness_sec": 86400,        # ingest runs every 24h
        "restart_on_staleness": True,  # enforced ingest schedule -- a missed run is a real fault
        "container": "event-radar",
    },
    {
        "name": "Study Platform",
        "business_impact": "low",    # internal tool
        "desc": "Supabase + pgvector RAG exam-prep",
        "icon": "school",
        "links": [("Private", "https://study.carsonng.com")],
        "monitor": True,
        "restart": "auto_heal",
        # Freshness reads Study's own `answer_log` usage table, NOT the shared
        # LLM ledger: practice-mode correct answers never call the LLM, so an
        # LLM-ledger signal read "idle" right after the user answered questions
        # (FIXED 2026-08-15). Every answered question writes an answer_log row.
        "project_tag": None,
        "freshness_table": "answer_log",
        "freshness_sec": 43200,        # user-driven usage, ~daily
        # Staleness here is "nobody studied recently", not a malfunction --
        # restarting the container cannot produce usage. Degrade the card, but
        # only a genuine liveness failure may restart this agent (FIXED 2026-08-15:
        # was auto-restarting every ~6min while idle and lock/re-lock cycling).
        "restart_on_staleness": False,
        "container": "study-app",
    },
    {
        "name": "Portfolio",
        "business_impact": "medium", # public-facing site
        "desc": "carsonng.com -- AI governance leadership positioning",
        "icon": "badge",
        # No GitHub link -- repo is private (confirmed: unauthenticated request 404s),
        # kept that way deliberately, not showing a link that 404s for anyone else.
        "links": [("Open", "https://carsonng.com")],
        "monitor": True,
        "restart": "alert_only",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
    {
        "name": "AI Regulation Radar",
        "business_impact": "medium", # public-facing compliance tracker
        "desc": "RegTech compliance tracker -- EU AI Act / NIST / HK PCPD",
        "icon": "gavel",
        # No GitHub link -- this repo has no remote configured at all (local-only git),
        # unlike every other service here. Not fabricating one.
        "links": [("Open", "https://regtech.carsonng.com"), ("Private", "https://regtech-private.carsonng.com")],
        "monitor": True,
        "restart": "alert_only",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
    {
        "name": "Change Impact Assessor",
        "business_impact": "low",    # demo
        "desc": "Human-in-the-loop AI risk gate for code/infra changes",
        "icon": "fact_check",
        "links": [("Demo", "https://carsonng.short.gy/change-impact-assessor"),
                  ("GitHub", "https://carsonng.short.gy/change-impact-assessor-github")],
        # No monitor: a GitHub PR page / Streamlit sleep-gate / HF Space aren't
        # reliable enough to be worth probing, and dropping them from the probe
        # loop also means fewer HTTP calls per refresh cycle.
        "monitor": False,
        "restart": "none",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
    {
        "name": "Sprint Analyzer",
        "business_impact": "low",    # demo
        "desc": "AI sprint retrospective generator",
        "icon": "assessment",
        "links": [("Demo", "https://carsonng.short.gy/sprint-analyzer"),
                  ("GitHub", "https://carsonng.short.gy/sprint-analyzer-carsonng")],
        "monitor": False,
        "restart": "none",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
    {
        "name": "AWS AI Code Review",
        "business_impact": "low",    # demo
        "desc": "Automated PR gate: Amazon Q + Inspector security scanning",
        "icon": "security",
        "links": [("Demo", "https://github.com/CarsonBytes/aws_code_review/pull/5"),
                  ("GitHub", "https://carsonng.short.gy/aws-code-review-github")],
        "monitor": False,
        "restart": "none",
        "project_tag": None,
        "freshness_sec": None,
        "container": None,
    },
]
