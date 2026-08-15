"""Standalone cross-project LLM usage dashboard -- ADDED 2026-07-16, fixed +
extended 2026-07-28.

Shows usage against the shared chatanywhere.tech + DeepSeek keys across
quant, study, and event-radar (and any future project that writes to the
same Supabase `llm_calls` table): which models, which projects, which call
types within each project, token spend, cost, and environment (paper/live
for quant).

FIXED 2026-07-28: originally read from a companion Supabase Edge Function
(usage-stats) that turned out to have never been deployed (404, silently
broken since creation). Now reads `llm_calls` directly via PostgREST + the
service-role key and aggregates in `ledger.py` -- see that module's
docstring. One less deployable, and this app already needs an httpx client.

Also added: a daily cost-threshold alert banner + Telegram push (`alerts.py`,
reuses D:\\quant\\analyst\\.env's TELEGRAM_BOT_TOKEN/CHAT_ID), an
auto-generated "biggest spender" insight line, and a projected-monthly-cost
KPI -- the actual FinOps/insight layer the original version never had.

ADDED 2026-08-14: the agent-cards strip became the start of a personal NOC --
two-layer health (liveness + Supabase-ledger readiness), blocked-by badges,
per-agent auto-restart with cooldown/lock, an incident log, and 7-day uptime,
all driven by `noc.py` (see its docstring). The health/restart loop runs as a
background task via asyncio.to_thread so it never blocks page rendering.

Run:  uv run python app.py      (then open http://localhost:8095)

Env (put in .env, see .env.example):
  SUPABASE_URL               required
  SUPABASE_SERVICE_ROLE_KEY  required
  ALERT_DAILY_COST_USD       optional, default 0.50
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional -- no push if unset
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import os

from nicegui import app, run, ui

import httpx

import alerts
import governance  # compliance radar engine (governance/engine.py)
import ledger  # both load .env themselves on import
import noc
import services

STATE: dict = {"data": None, "rows": None, "error": None, "days": 7, "last_fetch": None,
               "alert": None}

_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("ALERT_CHECK_INTERVAL_SEC", "900"))
_SERVICES_CHECK_INTERVAL_SEC = int(os.environ.get("SERVICES_CHECK_INTERVAL_SEC", "120"))
_RISK_LEDGER_INTERVAL_SEC = int(os.environ.get("RISK_LEDGER_INTERVAL_SEC", "300"))
_COMPLIANCE_INTERVAL_SEC = int(os.environ.get("COMPLIANCE_INTERVAL_SEC", "600"))

_PROJECT_COLORS = {"quant": "#16a34a", "study": "#2563eb", "events": "#9333ea", "(untagged)": "#6b7280"}


def fetch_stats(days: int) -> None:
    try:
        rows = ledger.fetch_rows(days)
        STATE["data"] = ledger.build_stats(rows, days)
        STATE["rows"] = rows
        STATE["error"] = None
        STATE["last_fetch"] = dt.datetime.now(dt.timezone.utc)  # aware UTC; displayed in HKT
        STATE["alert"] = alerts.run_check(ledger.today_cost(rows))
    except Exception as e:                          # noqa: BLE001
        STATE["error"] = str(e)


def _kpi(title: str, value: str, *, warn: bool = False) -> None:
    with ui.card().classes("min-w-[160px] grow" + (" bg-red-50" if warn else "")):
        ui.label(title).classes("text-xs text-grey-6")
        ui.label(value).classes("text-xl font-bold" + (" text-red-600" if warn else ""))


def _bar_chart(rows: list[dict], label_field: str, extra_fields: list[str] = None) -> None:
    if not rows:
        ui.label("(no data in this range)").classes("text-sm text-grey")
        return
    extra_fields = extra_fields or []
    labels = []
    for r in rows:
        parts = [str(r.get(label_field, "?"))] + [str(r.get(f, "")) for f in extra_fields if r.get(f)]
        labels.append(" ".join(p for p in parts if p))
    ui.echart({
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"fontSize": 10, "rotate": 20}},
        "yAxis": {"type": "value", "name": "calls"},
        "series": [{"type": "bar", "data": [r["calls"] for r in rows],
                    "itemStyle": {"color": "#2563eb"}}],
        "grid": {"left": 50, "right": 20, "top": 20, "bottom": 60},
    }).classes("w-full h-56")


def _efficiency_table(ranked: list[dict], label_field: str) -> None:
    if not ranked:
        ui.label("(no priced calls in this range)").classes("text-sm text-grey")
        return
    cols = [
        {"name": "label", "label": label_field.title(), "field": "label"},
        {"name": "per_1k_usd", "label": "$/1K tok", "field": "per_1k_usd", "sortable": True},
        {"name": "calls", "label": "Calls", "field": "calls", "sortable": True},
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "sortable": True},
    ]
    rows = [{"label": b[label_field], "per_1k_usd": f"{b['per_1k_usd']:.6f}",
             "calls": b["calls"], "cost_usd": f"{b['cost_usd']:.4f}"} for b in ranked]
    ui.table(columns=cols, rows=rows, row_key="label").classes("w-full").props("dense")


def _download_call_types_csv(data: dict) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["project", "call_type", "calls", "cost_usd", "cost_per_call",
                      "prompt_tokens", "completion_tokens"])
    for r in ledger.with_cost_per_call(data["by_call_type"]):
        writer.writerow([r["project"], r["call_type"], r["calls"], r["cost_usd"],
                          r["cost_per_call"], r["prompt_tokens"], r["completion_tokens"]])
    ui.download(buf.getvalue().encode(), filename=f"llm_usage_{data['range_days']}d.csv",
                media_type="text/csv")


@ui.refreshable
def alert_banner() -> None:
    alert = STATE.get("alert")
    if not alert or not alert["breached"]:
        return
    with ui.row().classes("w-full items-center gap-2 bg-red-100 border border-red-300 rounded p-3"):
        ui.icon("warning", color="red-600")
        ui.label(
            f"Today's LLM spend is ${alert['cost_today']:.4f}, over the ${alert['threshold']:.2f} "
            f"daily threshold."
        ).classes("text-red-800 font-medium")


@ui.refreshable
def noc_banner() -> None:
    """Persistent surface for a lock-alert Telegram delivery failure -- a
    failed push must never be the user's only warning (FIXED 2026-08-15:
    the Quant Paper lock alert recorded 'telegram failed' and nothing
    surfaced that anywhere except the incident log)."""
    pending = noc.get_pending_alerts()
    if not pending:
        return
    names = ", ".join(pending)
    with ui.row().classes("w-full items-center gap-2 bg-red-100 border border-red-300 rounded p-3"):
        ui.icon("report_problem", color="red-600")
        ui.label(
            f"NOC alert delivery failed for: {names}. Auto-heal has disabled itself on "
            f"these agents and the Telegram notification did not get through -- "
            f"retrying automatically; dismiss when acknowledged."
        ).classes("text-red-800 font-medium")
        ui.button("Dismiss", on_click=lambda: (noc.dismiss_pending(), refresh_all())) \
            .props("dense flat color=red")


def _impact_badge(impact: str | None) -> None:
    """Small colored chip for an agent's manually-assigned business_impact
    (services.py): red High / amber Med / green Low. No chip when unset."""
    styles = {"high": ("bg-red-100 text-red-700", "High"),
              "medium": ("bg-amber-100 text-amber-700", "Med"),
              "low": ("bg-green-100 text-green-700", "Low")}
    cls, text = styles.get(impact or "", (None, None))
    if cls:
        ui.label(text).classes(f"text-xs {cls} rounded px-1")


async def _mark_complied(rid: str) -> None:
    """Governance 'Mark as complied' action: DB work off the event loop, then
    re-render the tab from the refreshed snapshot cache. If the rule was
    auto-quarantining its agent (auto_action='quarantine') and no OTHER
    auto-quarantine target remains for that agent, resume the container --
    remediation ends the isolation (Task 4, Phase 3.2)."""
    rule = next((r for r in governance.cached_rules() if r["id"] == rid), None)
    ok = await run.io_bound(governance.mark_complied, rid)
    if ok and rule and rule.get("auto_action") == "quarantine" and rule.get("agent_slug"):
        agent = rule["agent_slug"]
        if agent not in governance.auto_quarantine_targets():
            svc = next((s for s in services.SERVICES if s["name"] == agent), None)
            if svc and svc.get("container") and await _proxy_action("unpause", svc["container"]):
                noc.unquarantine_agent(agent)
                ui.notify(f"{agent} resumed -- no overdue auto-quarantine rule left",
                          type="positive")
    governance_view.refresh()
    ui.notify("Rule marked complied + audited" if ok
              else "Mark-as-complied failed", type="positive" if ok else "negative")


async def _generate_report() -> None:
    """Task 6 (Phase 3.3): one-click compliance report -- aggregate rules,
    regulatory updates and the audit trail off the event loop, then download
    the Markdown. The evidence generator: 30 seconds, not three days."""
    ui.notify("Generating compliance report…", type="info")
    md = await run.io_bound(governance.build_report)
    fname = f"compliance-report-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    ui.download(md.encode(), filename=fname)


_RESTART_PROXY_URL = os.environ.get("RESTART_PROXY_URL", "http://restart-proxy:8096")


async def _proxy_action(action: str, container: str) -> bool:
    """Call the restart proxy (restart/pause/unpause). Off the event loop."""
    try:
        resp = await asyncio.to_thread(
            lambda: httpx.post(f"{_RESTART_PROXY_URL}/{action}",
                               json={"container": container}, timeout=30))
        return bool(resp.json().get("ok"))
    except Exception:                                    # noqa: BLE001
        return False


async def _quarantine(svc: dict) -> None:
    """Operator-initiated quarantine: pause the container via the proxy, then
    record it so the NOC stops auto-restarting the agent. Never automatic."""
    ok = await _proxy_action("pause", svc["container"])
    if ok:
        noc.quarantine_agent(svc["name"], reason="manual")
        alerts.send_telegram(f"\U0001f6d1 {svc['name']} quarantined (operator pause)",
                             tag="NOC", emoji="\U0001f6d1")
        ui.notify(f"{svc['name']} paused + quarantined", type="warning")
    else:
        ui.notify("Pause failed (proxy unreachable?)", type="negative")
    services_row.refresh()


async def _resume(svc: dict) -> None:
    """Lift a manual quarantine: unpause the container and clear the state."""
    ok = await _proxy_action("unpause", svc["container"])
    if ok:
        noc.unquarantine_agent(svc["name"])
        ui.notify(f"{svc['name']} resumed", type="positive")
    else:
        ui.notify("Resume failed (proxy unreachable?)", type="negative")
    services_row.refresh()


def _confirm_quarantine(svc: dict) -> None:
    """Confirmation dialog before pausing a container (disruptive, manual)."""
    with ui.dialog() as dialog, ui.card():
        ui.label(f"Quarantine {svc['name']}?").classes("font-bold")
        ui.label("The container will be PAUSED until you resume it manually. "
                 "Auto-restart is suppressed while quarantined.").classes("text-sm")
        with ui.row().classes("justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Quarantine", on_click=lambda: (dialog.close(), _quarantine(svc)))\
                .props("color=red")
    dialog.open()


@ui.refreshable
def governance_view() -> None:
    """Governance tab: Compliance Calendar, High-Impact Watchlist, Audit Trail.
    Renders a clear 'run the SQL' banner until the two governance tables exist
    (they cannot be created from this app -- PostgREST does no DDL).

    Render is NETWORK-FREE: it reads the snapshot cache the background
    compliance loop fills (governance.refresh_cache). Direct Supabase calls
    in render froze the event loop under rapid connections (FIXED 2026-08-15)."""
    if not governance.cached_tables_ready():
        with ui.row().classes("w-full items-center gap-2 bg-amber-50 border border-amber-300 rounded p-3"):
            ui.icon("construction", color="amber-700")
            ui.label(
                "Governance tables not created yet. Run the SQL in "
                "governance/migrations/001_governance_tables.sql in the Supabase SQL "
                "editor to enable this tab."
            ).classes("text-sm text-amber-900")
        return

    # --- Section 0a: Governance Mechanism Summary (Task 5, Phase 3.3) -------
    # Static narrative: plain-language explanation of how this system earns
    # trust -- for board members, auditors and new engineers alike. Written to
    # be read without any code knowledge.
    with ui.card().classes("w-full bg-blue-50 border border-blue-200"):
        with ui.column().classes("w-full gap-1 p-3"):
            ui.label("📖 Governance mechanism summary").classes("text-sm font-bold text-blue-900")
            ui.markdown(
                "This dashboard runs a **risk-led, policy-driven governance loop** over every "
                "automated agent (trading, event discovery, study tools):\n\n"
                "- **Impact-based risk grading** -- every agent carries a business-impact level "
                "(high / medium / low). High-impact agents are watched more closely; low-impact "
                "ones are counted but never over-scanned.\n"
                "- **Deterministic rule matching** -- regulatory updates are matched against "
                "explicit rules by code, not by a model's judgment. An LLM may help *read* a "
                "regulation; only code decides whether it *triggers* an action. No alert ever "
                "fires because a language model 'thought it looked relevant'.\n"
                "- **Automated task generation** -- when a regulation changes (e.g. the EU AI "
                "Act), a compliance task appears on the board automatically, with a deadline "
                "countdown. Nothing waits for someone to read an email.\n"
                "- **Policy-driven isolation** -- rules may opt in to auto-quarantine: an "
                "overdue obligation pauses the affected agent's container (with market-hours "
                "and operator controls), so a non-compliant system cannot silently keep running.\n"
                "- **Immutable audit trail** -- every status change, alert and override is "
                "recorded with a timestamp and actor. 'Show me the evidence' is one click away, "
                "not a memory.\n\n"
                "**The human-in-the-loop principle**: automation flags, isolates and documents; "
                "a human decides, remediates and signs off. This dashboard is the evidence "
                "layer, not the decision layer."
            ).classes("text-sm text-blue-900")
            with ui.row().classes("items-center gap-2 mt-1"):
                ui.button("Generate compliance report", icon="description",
                          on_click=_generate_report).props("dense color=primary")
                ui.label("Aggregates rules, regulatory updates and the audit trail into a "
                         "downloadable Markdown report.").classes("text-xs text-grey-6")

    # --- Section 0b: Compliance Health ring ----------------------------------
    compliance = governance.compliance_health()
    monitored = [s for s in services.SERVICES if s.get("monitor")]
    critical_n = sum(1 for s in monitored if compliance.get(s["name"]))
    ok_n = len(monitored) - critical_n
    with ui.row().classes("items-center gap-4 flex-wrap"):
        ui.echart({
            "tooltip": {"formatter": "{b}: {c}"},
            "series": [{
                "type": "pie", "radius": ["62%", "82%"], "avoidLabelOverlap": False,
                "label": {"show": False},
                "data": [
                    {"value": ok_n, "name": "Compliant", "itemStyle": {"color": "#16a34a"}},
                    {"value": critical_n, "name": "Overdue", "itemStyle": {"color": "#dc2626"}},
                ],
            }],
            "title": {"text": f"Compliance health: {ok_n}/{len(monitored)} agents clear",
                      "left": "center", "top": "88%",
                      "textStyle": {"fontSize": 11, "fontWeight": "normal"}},
        }).classes("w-40 h-36")
        with ui.column().classes("gap-1"):
            ui.label("Compliance health").classes("text-sm font-bold")
            ui.label(f"🟢 {ok_n} agent(s) compliant").classes("text-xs text-green-700")
            ui.label(f"🔴 {critical_n} agent(s) with overdue rules").classes("text-xs text-red-600")
            ui.label(f"{len(governance.cached_rules())} active rule(s) · "
                     f"{len(governance.cached_complied())} complied").classes("text-xs text-grey-6")

    # --- Section A: Compliance board (Trello-like) ---------------------------
    ui.label("Compliance board").classes("text-sm font-bold mt-4")
    now = dt.datetime.now(dt.timezone.utc)
    active = governance.cached_rules()
    complied = governance.cached_complied()

    def _rule_card(r: dict, status_label: str, badge_cls: str) -> None:
        deadline = governance._parse_ts(r.get("enforcement_deadline"))
        if deadline:
            days = (deadline - now).total_seconds() / 86400
            deadline_txt = f"{ledger.to_hkt(deadline):%m-%d %H:%M} ({int(days)}d left)" \
                if days >= 0 else f"{ledger.to_hkt(deadline):%m-%d %H:%M} (overdue {int(-days)}d)"
        else:
            deadline_txt = "no deadline"
        with ui.card().classes("w-full p-2"):
            ui.label(status_label).classes(f"text-xs {badge_cls} rounded px-1")
            ui.label(r["rule_name"]).classes("text-sm font-bold mt-1")
            ui.label(deadline_txt).classes("text-xs text-grey-6")
            if r["status"] != "COMPLIED":
                ui.button("Mark complied", on_click=lambda rid=r["id"]: _mark_complied(rid)) \
                    .props("dense flat color=positive").classes("mt-1")

    with ui.row().classes("w-full items-start gap-3 flex-nowrap"):
        for status, label, badge, rules_list in (
                ("PENDING", "Pending", "bg-amber-100 text-amber-700", active),
                ("OVERDUE", "Overdue", "bg-red-100 text-red-700", active),
                ("COMPLIED", "Complied", "bg-green-100 text-green-700", complied)):
            col_rules = [r for r in rules_list if r["status"] == status]
            with ui.column().classes("grow bg-grey-100 rounded p-2 min-h-[80px]"):
                ui.label(f"{label} ({len(col_rules)})").classes(
                    f"text-xs font-bold {badge} rounded px-1")
                if not col_rules:
                    ui.label("(none)").classes("text-xs text-grey-6")
                for r in col_rules:
                    _rule_card(r, label, badge)

    # --- Section B: High-Impact Watchlist -----------------------------------
    ui.label("High-impact watchlist").classes("text-sm font-bold mt-4")
    high = [s for s in services.SERVICES if s.get("business_impact") == "high"]
    anomalies = ledger.anomaly_counts()
    wcols = [
        {"name": "agent", "label": "Agent", "field": "agent", "sortable": True},
        {"name": "impact", "label": "Impact", "field": "impact"},
        {"name": "last_audit", "label": "Last audit check (HKT)", "field": "last_audit", "sortable": True},
        {"name": "anomalies", "label": "Anomalies (24h)", "field": "anomalies", "sortable": True},
    ]
    last = ledger.last_scan_at
    wrows = [{"agent": s["name"], "impact": s.get("business_impact", ""),
              "last_audit": ledger.to_hkt(last).strftime("%Y-%m-%d %H:%M:%S") if last else "—",
              "anomalies": anomalies.get(s["name"], 0)}
             for s in high]
    ui.table(columns=wcols, rows=wrows, row_key="agent").classes("w-full").props("dense")

    # --- Section C: Audit Trail ----------------------------------------------
    ui.label("Audit trail").classes("text-sm font-bold mt-4")
    audit = governance.get_audit_log()
    if not audit:
        ui.label("(no audit entries yet)").classes("text-sm text-grey")
    else:
        acols = [
            {"name": "ts", "label": "Time (HKT)", "field": "ts", "sortable": True},
            {"name": "rule", "label": "Rule", "field": "rule", "sortable": True},
            {"name": "action", "label": "Action", "field": "action", "sortable": True},
            {"name": "actor", "label": "Actor", "field": "actor", "sortable": True},
        ]
        arows = [{"ts": ledger.to_hkt(a["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                  "rule": a.get("rule_name", "—"), "action": a["action_taken"],
                  "actor": a.get("actor", "system")} for a in audit]
        ui.table(columns=acols, rows=arows, row_key="ts").classes("w-full").props(
            "dense max-height=240px")


@ui.refreshable
def services_row() -> None:
    # No "My Agents" heading: the cards render directly, nothing above them.
    # Equal-width / equal-height grid (FIXED 2026-08-15): the old flex row
    # let content-driven sizing compound with `grow`, so a card with more
    # links or more status lines rendered visibly wider and taller than its
    # siblings. CSS grid with auto-fill tracks makes every card in a row the
    # same width; grid items stretch to the row height by default; and a
    # reserved status slot (below) keeps quiet and busy cards the same size
    # instead of letting optional lines change the footprint. Uses a plain
    # div rather than ui.row so its own `display:flex` can't fight the grid.
    with ui.element("div").classes("w-full grid grid-cols-[repeat(auto-fill,minmax(250px,1fr))] gap-3"):
        compliance = governance.compliance_health()  # agent -> OVERDUE rule names (render-safe cache)
        for svc in services.SERVICES:
            status = noc.get_status(svc["name"])
            overdue = compliance.get(svc["name"], [])
            quarantined = bool(status and status.get("quarantined"))
            with ui.card().classes("w-full h-full min-h-[120px]"):
                with ui.row().classes("items-center gap-2"):
                    # The icon is identity, always neutral; the round dot
                    # carries the status -- a small classic health indicator
                    # right next to the name. Unmonitored cards get no dot.
                    ui.icon(svc["icon"], color="grey-600").classes("text-2xl")
                    if svc["monitor"]:
                        up = status["up"] if status else None
                        if overdue or quarantined:
                            # Compliance/operator quarantine outranks HTTP
                            # health: "operationally fine, compliance dead".
                            dot_color = "bg-red-500"
                        elif up is None:
                            dot_color = "bg-grey-400"       # not checked yet
                        elif not up:
                            dot_color = "bg-red-500"        # down
                        elif status and status["readiness"] == "stale":
                            # stale means different things depending on the
                            # agent class: enforced-cadence -> a real fault
                            # (amber); usage-driven (Study Platform) -> just
                            # idle, neutral tone, nothing wrong.
                            dot_color = "bg-amber-500" if svc.get("restart_on_staleness") \
                                else "bg-grey-500"
                        else:
                            dot_color = "bg-green-500"      # healthy
                        ui.element("div").classes(
                            f"w-2.5 h-2.5 rounded-full {dot_color} shrink-0")
                    ui.label(svc["name"]).classes("font-bold")
                    _impact_badge(svc.get("business_impact"))
                    if status and status.get("locked"):
                        ui.label("Locked").classes(
                            "text-xs bg-red-100 text-red-700 rounded px-1")
                ui.label(svc["desc"]).classes("text-xs text-grey-6")
                with ui.row().classes("items-center gap-3 mt-1 flex-wrap"):
                    for label, url in svc["links"]:
                        # Each link is one unit: the lock icon lives INSIDE the
                        # link element so it stays glued to its label (and is
                        # clickable with it) -- previously a separate 12px icon
                        # sat misaligned next to the text. gap-3 gives proper
                        # breathing room between links.
                        with ui.link(target=url, new_tab=True).classes(
                                "inline-flex items-center gap-0.5"):
                            if label == "Private":           # generic, off the label
                                ui.icon("lock", size="14px").classes("text-grey-6")
                            ui.label(label).classes("text-sm")
                # Reserved status slot: ALWAYS present, on every card, rendered
                # empty when there's nothing to say -- a busy card (down,
                # blocked by, uptime, clear-lock button) and a quiet one (or a
                # no-monitor demo card) keep the same footprint, so the grid
                # stays visually even without hiding any information.
                with ui.column().classes("w-full mt-1 min-h-[40px] justify-start"):
                    if svc["monitor"] and status:
                        if status["up"] is False:
                            ui.label("down").classes("text-xs text-red-600")
                        elif status["readiness"] == "stale":
                            if svc.get("restart_on_staleness"):
                                ui.label(f"degraded -- {status['readiness_detail'] or 'stale data'}")\
                                    .classes("text-xs text-amber-600")
                            else:
                                ui.label(f"idle -- {status['readiness_detail'] or 'no recent usage'}")\
                                    .classes("text-xs text-grey-6")
                        if status.get("blocked_by"):
                            ui.label("blocked by: " + ", ".join(status["blocked_by"])).classes(
                                "text-xs text-amber-700 bg-amber-50 rounded px-1 mt-1")
                        if status.get("uptime_7d") is not None:
                            ui.label(f"7d uptime: {status['uptime_7d']:.1f}%").classes(
                                "text-xs text-grey-6 mt-1")
                        if overdue:
                            ui.label("⚠ compliance overdue: " + ", ".join(overdue)).classes(
                                "text-xs text-red-600 mt-1")
                        if status.get("quarantined"):
                            reason = status["quarantined"]
                            ui.label("auto-quarantined (paused)" if reason == "compliance-auto"
                                     else "quarantined (paused)").classes(
                                "text-xs bg-red-100 text-red-700 rounded px-1 mt-1")
                            ui.button("Resume", on_click=lambda s=svc: _resume(s)) \
                                .props("dense flat color=positive")
                        elif svc.get("quarantinable") and svc.get("container"):
                            ui.button("Quarantine (pause)", on_click=lambda s=svc: _confirm_quarantine(s)) \
                                .props("dense flat color=red")
                        if svc["restart"] == "auto_heal" and status.get("locked"):
                            ui.button("Clear lock", on_click=lambda s=svc: (
                                noc.clear_lock(s["name"]), services_row.refresh())) \
                                .props("dense flat color=red")


@ui.refreshable
def incident_log() -> None:
    incidents = noc.get_incidents()
    if not incidents:
        return
    ui.label("Incident log").classes("text-sm font-bold mt-4")
    cols = [
        {"name": "ts", "label": "Time (HKT)", "field": "ts", "sortable": True},
        {"name": "agent", "label": "Agent", "field": "agent", "sortable": True},
        {"name": "event", "label": "Event", "field": "event", "sortable": True},
        {"name": "outcome", "label": "Outcome", "field": "outcome"},
        {"name": "detail", "label": "Detail", "field": "detail"},
    ]
    rows = [{"ts": ledger.to_hkt(i["ts"]).strftime("%Y-%m-%d %H:%M:%S"), "agent": i["agent"],
             "event": i["event"], "outcome": i.get("outcome", ""),
             "detail": i.get("detail", "")} for i in incidents]
    ui.table(columns=cols, rows=rows, row_key="ts").classes("w-full").props(
        "dense max-height=240px")


@ui.refreshable
def dashboard_body() -> None:
    if STATE["error"]:
        ui.label(f"⚠ {STATE['error']}").classes("text-red-600 font-bold")
        ui.button("Retry", on_click=lambda: (fetch_stats(STATE["days"]), refresh_all())) \
            .classes("mt-2")
        return
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return

    with ui.row().classes("items-center gap-2"):
        ui.label(f"Last refreshed: {ledger.to_hkt(STATE['last_fetch']):%H:%M:%S} (HKT)"
                 if STATE["last_fetch"] else "").classes("text-xs text-grey-6")

    with ui.tabs().classes("w-full") as tabs:
        overview_tab = ui.tab("Overview")
        cost_tab = ui.tab("Cost & Usage")
        reliability_tab = ui.tab("Reliability & Incidents")
        governance_tab = ui.tab("Governance")
    with ui.tab_panels(tabs, value=overview_tab).classes("w-full"):
        with ui.tab_panel(overview_tab):
            _overview_tab(data)
        with ui.tab_panel(cost_tab):
            _cost_tab(data)
        with ui.tab_panel(reliability_tab):
            _reliability_tab(data)
        with ui.tab_panel(governance_tab):
            governance_view()


def _overview_tab(data: dict) -> None:
    avg_daily = data["total_cost_usd"] / max(data["range_days"], 1)
    monthly_budget = alerts.ALERT_DAILY_COST_USD * 30
    projected_monthly = avg_daily * 30
    attribution = ledger.attribution_quality(data)

    with ui.row().classes("w-full flex-wrap gap-3 mt-2"):
        _kpi("Total calls", f"{data['total_calls']:,}")
        _kpi("Total cost", f"${data['total_cost_usd']:.4f}")
        _kpi("Prompt tokens", f"{data['total_prompt_tokens']:,}")
        _kpi("Completion tokens", f"{data['total_completion_tokens']:,}")
        _kpi("Projected monthly cost", f"${projected_monthly:.2f} / ${monthly_budget:.2f} budget",
             warn=projected_monthly > monthly_budget)
        _kpi("Cost attribution quality", f"{attribution['cost_tagged_pct']:.0f}% tagged by provider",
             warn=attribution["cost_tagged_pct"] < 50)

    insight = ledger.top_spender_insight(data)
    if insight:
        with ui.row().classes("w-full items-center gap-2 bg-blue-50 border border-blue-200 rounded p-2 mt-2"):
            ui.icon("lightbulb", color="blue-600")
            ui.label(insight).classes("text-sm text-blue-900")

    # 1-day range shows HOUR buckets (Task 1, Phase 3.0): a single
    # calendar-day bar hides the intraday shape entirely.
    if data["range_days"] == 1:
        dbp = data["hourly_by_project"]
        chart_title = "Cost per hour, by project"
    else:
        dbp = data["daily_by_project"]
        chart_title = "Cost per day, by project"
    if dbp["dates"]:
        ui.label(chart_title).classes("text-sm font-bold mt-4")
        project_series = [
            {"type": "line", "name": s["project"], "data": s["data"], "stack": "total",
             "smooth": True, "areaStyle": {}, "lineStyle": {"width": 1},
             "itemStyle": {"color": _PROJECT_COLORS.get(s["project"], "#6b7280")}}
            for s in dbp["series"]
        ]
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"top": 0, "textStyle": {"fontSize": 10}},
            "xAxis": {"type": "category", "data": dbp["dates"]},
            "yAxis": {"type": "value", "name": "cost (USD)"},
            "series": project_series + [
                {"type": "line", "name": "alert threshold",
                 "data": [alerts.ALERT_DAILY_COST_USD] * len(dbp["dates"]),
                 "lineStyle": {"type": "dashed", "color": "#dc2626", "width": 1}, "symbol": "none"},
            ],
            "grid": {"left": 50, "right": 20, "top": 40, "bottom": 30},
        }).classes("w-full h-64")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By provider (chatanywhere vs deepseek fallback in action)").classes("text-sm font-bold")
            _bar_chart(data["by_provider"], "provider")


def _cost_tab(data: dict) -> None:
    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By model").classes("text-sm font-bold")
            _bar_chart(data["by_model"], "model")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project & environment").classes("text-sm font-bold")
            _bar_chart(data["by_environment"], "project", ["environment"])

    ui.label("Model usage by project & call type").classes("text-sm font-bold mt-4")
    model_cols = [
        {"name": "project", "label": "Project", "field": "project", "sortable": True},
        {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
        {"name": "model", "label": "Model", "field": "model", "sortable": True},
        {"name": "calls", "label": "Calls", "field": "calls", "sortable": True},
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "sortable": True},
    ]
    model_rows = [{**r, "cost_usd": f"{r['cost_usd']:.4f}",
                   "_key": f"{r['project']}|{r['call_type']}|{r['model']}"}
                  for r in data["by_project_call_type_model"]]
    ui.table(columns=model_cols, rows=model_rows, row_key="_key").classes("w-full").props("dense")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Model efficiency ($/1K tokens, cheapest first)").classes("text-sm font-bold")
            _efficiency_table(ledger.efficiency_ranking(data["by_model"]), "model")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Provider efficiency ($/1K tokens, cheapest first)").classes("text-sm font-bold")
            _efficiency_table(ledger.efficiency_ranking(data["by_provider"]), "provider")

    legacy_usage = ledger.legacy_model_usage(data)
    if legacy_usage:
        with ui.row().classes("w-full items-center gap-2 bg-amber-50 border border-amber-300 rounded p-2 mt-4"):
            ui.icon("update", color="amber-700")
            pairs = ", ".join(f"{b['project']}:{b['call_type']}" for b in legacy_usage)
            ui.label(f"Still calling {ledger.LEGACY_MODEL} instead of {ledger.CURRENT_MODEL}: {pairs}") \
                .classes("text-sm text-amber-900")

    with ui.row().classes("w-full items-center justify-between mt-4"):
        ui.label("Call types by project").classes("text-sm font-bold")
        ui.button("Download CSV", icon="download", on_click=lambda: _download_call_types_csv(data)) \
            .props("flat dense")
    cols = [
        {"name": "project", "label": "Project", "field": "project", "sortable": True},
        {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
        {"name": "calls", "label": "Calls", "field": "calls", "sortable": True},
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "sortable": True},
        {"name": "cost_per_call", "label": "$/call", "field": "cost_per_call", "sortable": True},
        {"name": "prompt_tokens", "label": "Prompt tok", "field": "prompt_tokens", "sortable": True},
        {"name": "completion_tokens", "label": "Completion tok", "field": "completion_tokens", "sortable": True},
    ]
    call_types_with_avg = ledger.with_cost_per_call(data["by_call_type"])
    rows = [{**r, "cost_usd": f"{r['cost_usd']:.4f}", "cost_per_call": f"{r['cost_per_call']:.6f}"}
            for r in call_types_with_avg]
    ui.table(columns=cols, rows=rows, row_key="call_type").classes("w-full").props("dense")


def _reliability_tab(data: dict) -> None:
    ui.label("Slowest call types (avg latency)").classes("text-sm font-bold")
    latency_ranked = ledger.latency_ranking(data["by_call_type"])
    if latency_ranked:
        lat_cols = [
            {"name": "project", "label": "Project", "field": "project", "sortable": True},
            {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
            {"name": "avg_latency_ms", "label": "Avg latency (ms)", "field": "avg_latency_ms", "sortable": True},
            {"name": "calls", "label": "Calls", "field": "calls", "sortable": True},
        ]
        ui.table(columns=lat_cols, rows=latency_ranked[:10], row_key="call_type").classes("w-full").props("dense")
    else:
        ui.label("(no latency data in this range)").classes("text-sm text-grey")

    incident_log()

    history = alerts.get_history()
    if history:
        ui.label("Alert history").classes("text-sm font-bold mt-4")
        hist_cols = [
            {"name": "fired_at", "label": "Fired at (HKT)", "field": "fired_at", "sortable": True},
            {"name": "cost_today", "label": "Cost that day", "field": "cost_today", "sortable": True},
            {"name": "threshold", "label": "Threshold", "field": "threshold"},
        ]
        hist_rows = [{"fired_at": ledger.to_hkt(h["fired_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                      "cost_today": f"${h['cost_today']:.4f}", "threshold": f"${h['threshold']:.2f}"}
                     for h in history]
        ui.table(columns=hist_cols, rows=hist_rows, row_key="fired_at").classes("w-full").props("dense")


def refresh_all() -> None:
    alert_banner.refresh()
    noc_banner.refresh()
    services_row.refresh()
    incident_log.refresh()
    dashboard_body.refresh()


def _refresh_safely(*refreshables) -> None:
    """Refresh module-level refreshables from a background loop without
    letting a deleted-client race (nicegui issue #3028 -- 'Client has been
    deleted but is still being used') raise through the loop. Also skips the
    work when no browser is connected, since the refreshables have no client
    to render into anyway."""
    try:
        from nicegui import Client
        if not any(c.has_socket_connection for c in Client.instances):
            return
    except Exception:                                  # noqa: BLE001
        pass
    for fn in refreshables:
        try:
            fn.refresh()
        except RuntimeError:                           # noqa: BLE001
            pass


async def _alert_check_loop() -> None:
    """Runs regardless of whether a browser tab is open (an app-startup
    background task, not tied to any page/client) so a Telegram push can
    fire even if nobody's looking at the dashboard right now. httpx calls in
    fetch_stats are sync, so run them off the event loop thread."""
    while True:
        await asyncio.sleep(_ALERT_CHECK_INTERVAL_SEC)
        await asyncio.to_thread(fetch_stats, STATE["days"])
        _refresh_safely(alert_banner)


async def _services_check_loop() -> None:
    while True:
        await asyncio.to_thread(noc.refresh_health)
        _refresh_safely(noc_banner, services_row, incident_log)
        await asyncio.sleep(_SERVICES_CHECK_INTERVAL_SEC)


async def _risk_ledger_loop() -> None:
    """Risk-ledger scan (Phase 3): runs off the event loop so a slow Supabase
    call never blocks rendering; separate task so it can't stall the other
    loops (same resilience pattern as the existing per-loop tasks). Work
    first, then sleep, so the very first scan happens at startup."""
    while True:
        await asyncio.to_thread(ledger.scan_high_impact_calls)
        await asyncio.sleep(_RISK_LEDGER_INTERVAL_SEC)


async def _compliance_loop() -> None:
    """Compliance radar (Phase 2): deadline enforcement + rule matching.
    Same isolation as _risk_ledger_loop -- each loop hangs independently at
    worst, never together. Work first, then sleep, so the UI snapshot cache
    (governance.refresh_cache) is populated at startup -- the tab renders
    from that cache, never from network calls."""
    while True:
        await asyncio.to_thread(governance.check_pending_rules)
        await asyncio.sleep(_COMPLIANCE_INTERVAL_SEC)


@ui.page("/")
def main_page() -> None:
    dark_mode = ui.dark_mode()

    def _toggle_dark() -> None:
        dark_mode.value = not dark_mode.value
        dark_toggle.props(f"icon={'light_mode' if dark_mode.value else 'dark_mode'}")

    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4"):
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
            ui.label("Command Deck").classes("text-2xl font-bold")
            with ui.row().classes("items-center gap-2"):
                dark_toggle = ui.button(icon="dark_mode", on_click=_toggle_dark).props("flat round")
                ui.button("Refresh", icon="refresh",
                          on_click=lambda: (fetch_stats(STATE["days"]), refresh_all())) \
                    .props("color=primary")
        ui.label("AI Governance Professional (AIGP) class -- cross-project usage: "
                 "quant (paper + live) + study + event-radar, reading directly from "
                 "the shared Supabase llm_calls ledger.").classes("text-sm text-grey-6")

        alert_banner()
        noc_banner()
        services_row()

        def _set_range(e) -> None:
            STATE["days"] = e.value
            fetch_stats(STATE["days"])
            refresh_all()

        # Range + alert-threshold live UNDER the agent cards (layout request
        # 2026-08-16): the cards are the dashboard's identity, the controls
        # tune the charts below them.
        with ui.row().classes("items-center gap-2 mt-1"):
            ui.label("Range:").classes("text-sm")
            ui.toggle({1: "Today", 7: "7d", 30: "30d", 90: "90d"}, value=STATE["days"],
                      on_change=_set_range).props("dense")

        with ui.row().classes("items-center gap-2"):
            ui.label("Alert threshold ($/day):").classes("text-sm")
            threshold_input = ui.number(value=alerts.ALERT_DAILY_COST_USD, min=0, step=0.05,
                                        format="%.2f").props("dense outlined").classes("w-24")

            def _save_threshold() -> None:
                alerts.set_daily_threshold(threshold_input.value)
                ui.notify(f"Alert threshold set to ${threshold_input.value:.2f}", type="positive")
                refresh_all()

            ui.button("Save", on_click=_save_threshold).props("dense flat")
        ui.separator().classes("my-2")
        dashboard_body()

    fetch_stats(STATE["days"])
    refresh_all()


app.on_startup(lambda: asyncio.create_task(_alert_check_loop()))
app.on_startup(lambda: asyncio.create_task(_services_check_loop()))
app.on_startup(lambda: asyncio.create_task(_risk_ledger_loop()))
app.on_startup(lambda: asyncio.create_task(_compliance_loop()))

ui.run(title="Command Deck", favicon="💰", port=int(os.environ.get("PORT", "8095")), reload=False, show=False)
