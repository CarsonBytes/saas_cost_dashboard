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
                                 Platform) and agents that are monitored but
                                 never auto-restarted (Quant Live) stay False.
                                 Restart-authority ONLY -- does not drive the
                                 card's display (see enforced_cadence below).
                                 Default False.
  enforced_cadence             -- whether staleness here represents a REAL
                                 FAULT for display purposes (amber "degraded"
                                 + an alert) vs normal idle behavior (grey
                                 "idle", nothing wrong). Deliberately separate
                                 from restart_on_staleness (ADDED 2026-08-17):
                                 Quant Live has a real enforced write cadence
                                 (its staleness IS a fault worth showing and
                                 alerting on) but must never auto-restart, so
                                 the two fields diverge for it -- True/False --
                                 where every other monitored agent so far had
                                 them match. Default False.
  environment_tag              -- optional narrowing of project_tag's freshness
                                 lookup to one `environment` value in the
                                 shared llm_calls ledger (ADDED 2026-08-17).
                                 Needed the moment two agents share a
                                 project_tag: Quant Paper and Quant Live both
                                 write project="quant", split only by
                                 environment="paper"/"live" -- without this,
                                 monitoring both at once would let each one's
                                 freshness leak into the other's.
  market_hours_only            -- whether this agent's readiness check (and,
                                 for auto_heal agents, its staleness-triggered
                                 restart eligibility and auto-quarantine)
                                 should be skipped outside the real NYSE
                                 session (ADDED 2026-08-17, replacing a
                                 hardcoded "is this literally Quant Paper by
                                 name" check in noc.py -- Quant Live trades the
                                 same session and needed the same exception
                                 without noc.py growing a second hardcoded
                                 name). Default False.
  container                   -- Docker container name to restart (auto_heal).
                                 Also the manual pause target UNLESS
                                 quarantine_containers overrides it. A
                                 monitored agent that's alert_only may still
                                 set this purely so the manual pause action
                                 works.
  quarantine_containers        -- optional list overriding which container(s)
                                 the manual Quarantine/Resume action pauses
                                 (ADDED 2026-08-18). Defaults to [container]
                                 when absent. Needed when one card represents
                                 more than one running container -- Event
                                 Radar's public demo (events-demo.carsonng.com)
                                 runs as its own separate container alongside
                                 the private instance, and quarantining
                                 "Event Radar" while leaving the public demo
                                 serving traffic defeats the point, especially
                                 for a compliance/policy-driven quarantine.
                                 Auto-heal is UNAFFECTED by this -- it always
                                 targets `container` only, since the demo has
                                 no readiness signal of its own.
  quarantinable               -- whether the UI offers a manual "Quarantine
                                 (pause)" action for this agent (operator
                                 decision, never automatic). Only agents with
                                 a container on the restart proxy's allow-list
                                 can be quarantined. Default False.

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
        "environment_tag": "paper",     # shares project_tag "quant" with Live -- split by environment
        "freshness_sec": 900,          # writes every ~1min during market hours
        "market_hours_only": True,     # no new scans expected outside the NYSE session
        "restart_on_staleness": True,  # enforced scan loop -- a stalled loop is a real fault
        "enforced_cadence": True,      # matches restart_on_staleness here -- display + restart agree
        "container": "quant-dashboard-docker",
        "quarantinable": True,  # manual pause is an operator call, never automatic
    },
    {
        # MIGRATED to Docker 2026-08-17 (quant-dashboard-live-docker, its own
        # quant-ibgateway-live-docker sidecar, restart:unless-stopped in
        # D:\quant\docker-compose.live.yml -- confirmed live before writing
        # this). Monitored like every other agent now that it's containerized,
        # but deliberately kept off auto-heal: this dashboard's restart logic
        # doesn't understand IBKR's reconciliation state well enough to safely
        # bounce a live trading agent on an automated staleness inference, and
        # Docker's own restart:unless-stopped already covers a genuine crash.
        # alert_only gives full visibility (liveness, readiness, uptime,
        # blocked-by) without ever taking automated action on real money.
        "name": "Quant Trading (Live)",
        "business_impact": "high",   # live 17-ETF trading
        "desc": "Live 17-ETF trading, Docker deployment",
        "icon": "show_chart",
        # Access-gated like the other Private links, so the card labels it
        # Private (the generic renderer adds the lock icon off the label).
        "links": [("Private", "https://quant-live.carsonng.com")],
        "monitor": True,
        "restart": "alert_only",
        "project_tag": "quant",
        "environment_tag": "live",      # shares project_tag "quant" with Paper -- split by environment
        "freshness_sec": 900,           # same board_scan cadence as Paper
        "market_hours_only": True,      # no new scans expected outside the NYSE session
        "restart_on_staleness": False,  # alert_only -- never auto-restarts, regardless
        "enforced_cadence": True,       # staleness here IS a real fault (unlike Study Platform's idle)
        "container": "quant-dashboard-live-docker",  # not for auto-heal -- enables manual pause only
        "quarantinable": True,  # manual pause is an operator call, never automatic
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
        "enforced_cadence": True,      # matches restart_on_staleness here -- display + restart agree
        "container": "event-radar",    # auto-heal target only -- readiness/freshness tracks this one
        # ADDED 2026-08-18: "Event Radar" presents as one card but runs as TWO
        # containers -- event-radar (private) and event-radar-demo (public,
        # events-demo.carsonng.com). Quarantining the card now pauses both
        # together; leaving the public demo running while "Event Radar" shows
        # quarantined was found live to be actively misleading -- confirmed
        # via docker ps that the demo kept serving traffic throughout. A
        # compliance- or policy-driven quarantine in particular needs the
        # PUBLIC instance paused at least as much as the private one. Does
        # NOT affect auto-heal, which stays scoped to `container` above --
        # the demo has no readiness signal of its own to justify restarting it.
        "quarantine_containers": ["event-radar", "event-radar-demo"],
        "quarantinable": True,  # manual pause is an operator call, never automatic
    },
    {
        "name": "Study Platform",
        "business_impact": "low",    # internal tool
        "desc": "Supabase + pgvector RAG exam-prep",
        "icon": "school",
        # ADDED 2026-08-29: study-demo (container, port 8092, study-demo.carsonng.com)
        # -- same split as Event Radar's Demo/Private pair. Verified live before
        # wiring up: both localhost:8092 and the public hostname return 200.
        "links": [("Demo", "https://study-demo.carsonng.com"),
                  ("Private", "https://study.carsonng.com")],
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
        "enforced_cadence": False,     # matches restart_on_staleness here -- idle, not a fault
        "container": "study-app",      # auto-heal target only -- readiness tracks this one
        # Same reasoning as Event Radar's pairing (2026-08-18): quarantining
        # "Study Platform" should pause the public demo too, not just the
        # private instance -- auto-heal stays scoped to `container` above.
        "quarantine_containers": ["study-app", "study-demo"],
        "quarantinable": True,  # manual pause is an operator call, never automatic
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
