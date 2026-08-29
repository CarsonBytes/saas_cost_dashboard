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
import logging
import os

from nicegui import app, run, ui

import httpx

import alerts
import commands  # Telegram control plane (/unlock /status /mute)
import governance  # compliance radar engine (governance/engine.py)
import ledger  # both load .env themselves on import
import noc
import services

log = logging.getLogger("command-deck")

STATE: dict = {"data": None, "rows": None, "error": None, "days": 7, "last_fetch": None,
               "alert": None,
               # ADDED 2026-08-26 (Phase 4): per-project drill-down filter
               # ("quant"/"events"/"study"/...; None = all projects), the
               # preceding equal-length window's totals for KPI deltas, and
               # an optional custom HKT date range (start, end) overriding
               # the trailing-days toggle.
               "project": None, "prev": None, "custom": None, "preset_days": 7,
               "active_tab": "Overview"}

_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("ALERT_CHECK_INTERVAL_SEC", "900"))
_SERVICES_CHECK_INTERVAL_SEC = int(os.environ.get("SERVICES_CHECK_INTERVAL_SEC", "120"))
_RISK_LEDGER_INTERVAL_SEC = int(os.environ.get("RISK_LEDGER_INTERVAL_SEC", "300"))
_COMPLIANCE_INTERVAL_SEC = int(os.environ.get("COMPLIANCE_INTERVAL_SEC", "600"))

_PROJECT_COLORS = {"quant": "#16a34a", "study": "#2563eb", "events": "#9333ea", "(untagged)": "#6b7280"}

# Agent card -> ledger project tag, for the per-project drill-down (A1):
# which cards' costs are actually visible in the shared ledger. Quant Paper
# and Live share project_tag "quant" (split by environment, not by project),
# and Study Platform writes project="study" even though its freshness signal
# comes from answer_log.
_CARD_PROJECTS = {"Quant Trading (Paper)": "quant", "Quant Trading (Live)": "quant",
                  "Event Radar": "events", "Study Platform": "study"}


@ui.refreshable
def project_filter_chip() -> None:
    """Active per-project cost filter, shown just above the tab strip. Empty
    when no filter is on."""
    project = STATE.get("project")
    if not project:
        return
    with ui.row().classes("items-center gap-2 bg-blue-50 border border-blue-200 rounded px-3 py-1"):
        ui.icon("filter_alt", color="blue-600").classes("text-sm")
        ui.label(f"Costs filtered to: {project}").classes("text-sm text-blue-900")
        ui.button(icon="close", on_click=lambda: _set_project(None)) \
            .props("dense flat round size=sm color=blue-600").tooltip("Clear filter")


@ui.refreshable
def burn_bar() -> None:
    """Thin budget-burn bar under the header — always visible so projected
    vs budget is glanceable without opening the Overview tab."""
    data = STATE.get("data")
    if not data:
        return
    avg_daily = data["total_cost_usd"] / max(data["range_days"], 1)
    projected = avg_daily * 30
    budget = alerts.effective_monthly_budget()
    pct = min(100, int(projected / budget * 100)) if budget else 0
    days_left = max(0, 30 - data["range_days"])
    color = "bg-red-500" if projected > budget else "bg-violet-600"
    with ui.element("div").classes("w-full h-1.5 bg-zinc-100 rounded-full overflow-hidden flex"):
        ui.element("div").classes(f"h-full {color} transition-all").style(f"width:{pct}%")
        if pct < 100 and pct > 0:
            ui.element("div").classes("h-full w-0.5 bg-red-500")
    with ui.row().classes("w-full justify-between items-center mt-1"):
        ui.label(f"Projected ${projected:.2f} / ${budget:.2f} budget · {pct}% burned · {days_left}d left in window").classes(
            "text-[11px] font-mono " + ("text-red-600" if projected > budget else "text-zinc-600"))
        ui.label("Click → Cost tab  ·  Cmd+K palette  ·  g o/c/r/g").classes("text-[11px] text-zinc-400")


def fetch_stats(days: int | None = None) -> None:
    """Fetch + aggregate the active window: the custom HKT date range when one
    is set in STATE, otherwise the trailing-`days` window. Also fetches the
    PRECEDING equal-length window for the KPI deltas -- two paginated
    PostgREST calls instead of one; still off the event loop (callers run this
    in a thread). The background alert loop calls this with no args and keeps
    refreshing whatever window is currently active.

    The alert check always sees the UNFILTERED rows: the daily threshold is a
    global figure and must not change meaning while a project filter is on.
    """
    try:
        days = days or STATE["days"]
        custom = STATE.get("custom")
        if custom:
            rows, prev_rows = ledger.fetch_rows_custom(*custom)
            days = (dt.date.fromisoformat(custom[1])
                    - dt.date.fromisoformat(custom[0])).days + 1
        else:
            rows, prev_rows = ledger.fetch_rows_and_previous(days)
        project = STATE.get("project")
        data_rows = [r for r in rows if r.get("project") == project] if project else rows
        STATE["data"] = ledger.build_stats(data_rows, days)
        STATE["rows"] = rows
        STATE["days"] = days
        STATE["prev"] = ledger.window_totals(prev_rows)
        STATE["error"] = None
        STATE["last_fetch"] = dt.datetime.now(dt.timezone.utc)  # aware UTC; displayed in HKT
        STATE["alert"] = alerts.run_check(ledger.today_cost(rows))
    except Exception as e:                          # noqa: BLE001
        STATE["error"] = str(e)


async def _set_project(project: str | None) -> None:
    """Set/clear the per-project drill-down filter and refetch (the aggregate
    runs on filtered rows server-side of the UI, so every chart, table, KPI
    and CSV export follows the filter with no further plumbing).

    FIXED 2026-08-30: fetch_stats() does two paginated Supabase round-trips
    and its own docstring says plainly "callers run this in a thread" --
    this (and five other call sites) called it directly instead, which
    blocks NiceGUI's single shared event loop, freezing the app for EVERY
    connected browser tab, not just the one that clicked, for however long
    those two round-trips take."""
    STATE["project"] = project
    await asyncio.to_thread(fetch_stats)
    refresh_all()


def _kpi(title: str, value: str, *, warn: bool = False,
         sub: str | None = None, sub_cls: str = "text-grey-6",
         spark: list[float] | None = None) -> None:
    """One KPI card. `sub` is a small secondary line (e.g. a
    period-over-period delta); spark is a tiny inline trend. The card sits
    in the overview tab's responsive grid -- 1 col on phones, 2 on tablets,
    3 on desktop -- instead of the old flex row whose `min-w grow` cards
    wrapped unevenly at 375px."""
    with ui.card().classes("w-full" + (" bg-red-50" if warn else "")):
        ui.label(title).classes("text-xs text-grey-6")
        ui.label(value).classes("text-xl font-bold" + (" text-red-600" if warn else ""))
        if sub:
            ui.label(sub).classes(f"text-xs {sub_cls}")
        if spark and len(spark) > 1:
            ui.echart({
                "xAxis": {"type": "category", "data": [str(i) for i in range(len(spark))], "show": False},
                "yAxis": {"show": False},
                "grid": {"left": 0, "right": 0, "top": 2, "bottom": 2},
                "series": [{"type": "line", "data": spark, "smooth": True, "symbol": "none",
                            "lineStyle": {"width": 1.5, "color": "#7c3aed" if warn else "#16a34a"},
                            "areaStyle": {"opacity": 0.12, "color": "#7c3aed" if warn else "#16a34a"}}],
            }).classes("w-full h-6 -mb-1").props("auto-resize")


def _delta_sub(current: float, previous: float | None, *, lower_is_better: bool = False) -> tuple[str, str] | None:
    """Period-over-period delta line for a KPI: ('↑ 23% vs prev 7d', cls).
    None when there's no prior window or a zero baseline (a percentage off
    zero would be meaningless). Cost deltas are colored by whether they're
    good news; call/token counts stay neutral grey."""
    if not previous:
        return None
    pct = (current - previous) / previous * 100
    arrow = "↑" if pct >= 0 else "↓"
    label = f"{arrow} {abs(pct):.0f}% vs prev {STATE['days']}d"
    if lower_is_better:
        cls = "text-red-600" if pct > 0 else ("text-green-700" if pct < 0 else "text-grey-6")
        return label, cls
    return label, "text-grey-6"


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


def _download_csv(filename: str, header: list[str], rows: list[list]) -> None:
    """One generic CSV download for every export button (call-types, model
    usage, latency) -- previously only call-types had one."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    ui.download(buf.getvalue().encode(), filename=filename, media_type="text/csv")


def _download_call_types_csv(data: dict) -> None:
    rows = [[r["project"], r["call_type"], r["calls"], r["cost_usd"],
             r["cost_per_call"], r["prompt_tokens"], r["completion_tokens"]]
            for r in ledger.with_cost_per_call(data["by_call_type"])]
    _download_csv(f"llm_usage_{data['range_days']}d.csv",
                  ["project", "call_type", "calls", "cost_usd", "cost_per_call",
                   "prompt_tokens", "completion_tokens"], rows)


def _download_model_usage_csv(data: dict) -> None:
    _download_csv(f"model_usage_{data['range_days']}d.csv",
                  ["project", "call_type", "model", "calls", "cost_usd"],
                  [[r["project"], r["call_type"], r["model"], r["calls"], r["cost_usd"]]
                   for r in data["by_project_call_type_model"]])


def _download_latency_csv(data: dict) -> None:
    ranked = ledger.latency_ranking(data["by_call_type"])
    _download_csv(f"latency_{data['range_days']}d.csv",
                  ["project", "call_type", "avg_latency_ms", "calls"],
                  [[r["project"], r["call_type"], r["avg_latency_ms"], r["calls"]]
                   for r in ranked])


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


@ui.refreshable
def maintenance_banner() -> None:
    """Active maintenance window (Phase 5 A3): NOC actions suppressed so a
    deploy bouncing containers doesn't trip restarts/uptime. Amber, with one-
    click resume."""
    remaining = noc.maintenance_remaining_min()
    if remaining is None:
        return
    with ui.row().classes("w-full items-center gap-2 bg-amber-50 border border-amber-300 rounded p-3"):
        ui.icon("build", color="amber-700")
        ui.label(f"Monitoring paused ({remaining} min remaining) -- restarts, "
                 f"alerts and uptime recording are suppressed.").classes(
            "text-amber-900 font-medium")
        ui.button("Resume monitoring now",
                  on_click=lambda: (noc.clear_maintenance(), refresh_all())) \
            .props("dense flat color=amber-8").mark("resume-mute")


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


def _quarantine_targets(svc: dict) -> list[str]:
    """Which container(s) a manual Quarantine/Resume touches -- usually just
    `container`, but a card can represent more than one running container
    (ADDED 2026-08-18: Event Radar's public demo runs as its own separate
    container alongside the private instance; see services.py's
    quarantine_containers docs)."""
    return svc.get("quarantine_containers") or [svc["container"]]


async def _quarantine(svc: dict) -> None:
    """Operator-initiated quarantine: pause every container in this agent's
    group via the proxy, then record it so the NOC stops auto-restarting the
    agent. Never automatic. All-or-nothing feedback: if any container in the
    group fails to pause, say exactly which one -- a silent partial pause
    (e.g. the private instance paused but the public demo still running)
    would be worse than an obvious failure."""
    containers = _quarantine_targets(svc)
    failed = [c for c in containers if not await _proxy_action("pause", c)]
    if not failed:
        noc.quarantine_agent(svc["name"], reason="manual")
        alerts.send_telegram(f"\U0001f6d1 {svc['name']} quarantined (operator pause): "
                             f"{', '.join(containers)}", tag="NOC", emoji="\U0001f6d1")
        ui.notify(f"{svc['name']} paused + quarantined", type="warning")
    else:
        ui.notify(f"Pause failed for: {', '.join(failed)} (proxy unreachable?)", type="negative")
    services_row.refresh()


async def _resume(svc: dict) -> None:
    """Lift a manual quarantine: unpause every container in the group and
    clear the state. Same all-or-nothing container-level feedback as
    _quarantine() -- naming which container failed (FIXED 2026-08-18: this
    used to just say "proxy unreachable?" with no indication of which
    container, or even whether the underlying pause/unpause itself failed
    versus the response merely not making it back)."""
    containers = _quarantine_targets(svc)
    failed = [c for c in containers if not await _proxy_action("unpause", c)]
    if not failed:
        noc.unquarantine_agent(svc["name"])
        ui.notify(f"{svc['name']} resumed", type="positive")
    else:
        ui.notify(f"Resume failed for: {', '.join(failed)} (proxy unreachable? "
                  f"if it recovers on its own shortly, the card will catch up "
                  f"automatically)", type="negative")
    services_row.refresh()


def _passcode_dialog(svc: dict, *, action: str) -> None:
    """Shared confirm+passcode dialog for both manual container actions
    (FIXED 2026-08-17: neither Quarantine nor Resume required anything
    beyond a confirm-dialog click, on a dashboard the open internet can
    reach -- anyone could pause or resume a live trading agent's container.
    Every attempt, right or wrong, is checked and logged server-side via
    noc.check_quarantine_passcode(); the passcode itself is never compared
    client-side."""
    is_quarantine = action == "quarantine"
    verb = "Quarantine (pause)" if is_quarantine else "Resume"
    with ui.dialog() as dialog, ui.card():
        ui.label(f"{verb} {svc['name']}?").classes("font-bold")
        if is_quarantine:
            ui.label("The container will be PAUSED until you resume it manually. "
                     "Auto-restart is suppressed while quarantined.").classes("text-sm")
        else:
            ui.label("The container will be UNPAUSED and normal monitoring "
                     "resumes.").classes("text-sm")
        passcode_input = ui.input("Passcode", password=True, password_toggle_button=True) \
            .props("dense outlined autofocus").classes("w-full mt-2")
        error_label = ui.label("").classes("text-xs text-red-600 mt-1")

        async def _submit() -> None:
            allowed, reason = noc.check_quarantine_passcode(svc["name"], passcode_input.value)
            if not allowed:
                # FIXED 2026-08-17 (found during verification): this used to
                # also call services_row.refresh() here to reflect a fresh
                # lockout immediately -- but this dialog is rendered inside
                # that same refreshable's tree, so refreshing it while the
                # dialog is still open destroyed the dialog along with it,
                # taking the error message with it before the operator could
                # read it or retry. The card's auth-locked state still shows
                # up on the next natural refresh (background health loop, or
                # closing this dialog); staying open with the error visible
                # matters more than that state being instantly reflected.
                error_label.text = reason
                passcode_input.value = ""
                return
            dialog.close()
            await (_quarantine(svc) if is_quarantine else _resume(svc))
            # _quarantine()/_resume() each already call services_row.refresh()
            # at the end -- no need to duplicate it here.

        passcode_input.on("keydown.enter", _submit)
        with ui.row().classes("justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(verb, on_click=_submit).props("color=red" if is_quarantine else "color=positive")
    dialog.open()


def _confirm_quarantine(svc: dict) -> None:
    _passcode_dialog(svc, action="quarantine")


def _confirm_resume(svc: dict) -> None:
    _passcode_dialog(svc, action="resume")


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
            if days < 0:
                deadline_txt = f"{ledger.to_hkt(deadline):%m-%d %H:%M} (overdue {int(-days)}d)"
                due_cls = "text-red-600 bg-red-50 border border-red-200 rounded px-1"
            elif days <= 7:
                deadline_txt = f"{ledger.to_hkt(deadline):%m-%d %H:%M} ({int(days)}d left)"
                due_cls = "text-amber-700 bg-amber-50 border border-amber-200 rounded px-1"
            else:
                deadline_txt = f"{ledger.to_hkt(deadline):%m-%d %H:%M} ({int(days)}d left)"
                due_cls = "text-grey-6"
        else:
            deadline_txt = "no deadline"
            due_cls = "text-grey-6"
        with ui.card().classes("w-full p-2"):
            ui.label(status_label).classes(f"text-xs {badge_cls} rounded px-1")
            ui.label(r["rule_name"]).classes("text-sm font-bold mt-1")
            ui.label(deadline_txt).classes(f"text-xs {due_cls}")
            if r["status"] != "COMPLIED":
                ui.button("Mark complied", on_click=lambda rid=r["id"]: _mark_complied(rid)) \
                    .props("dense flat color=positive").classes("mt-1")

    with ui.row().classes("w-full items-start gap-3 flex-nowrap"):
        for status, label, badge, rules_list in (
                ("PENDING", "Pending", "bg-amber-100 text-amber-700", active),
                ("OVERDUE", "Overdue", "bg-red-100 text-red-700", active),
                ("COMPLIED", "Complied", "bg-green-100 text-green-700", complied)):
            col_rules = [r for r in rules_list if r["status"] == status]
            with ui.column().classes("grow bg-grey-2 rounded p-2 min-h-[80px]"):
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
    # Filter input lives OUTSIDE the nested refreshable table for the same
    # focus-loss reason as the incident log's.
    ui.label("Audit trail").classes("text-sm font-bold mt-4")
    audit = governance.get_audit_log()

    def _render_audit(q: str) -> None:
        q = q.lower()
        entries = [a for a in audit
                   if not q or q in a.get("rule_name", "").lower()
                   or q in a["action_taken"].lower() or q in a.get("actor", "").lower()]
        if not entries:
            ui.label("(no matching audit entries)" if q else "(no audit entries yet)") \
                .classes("text-sm text-grey")
            return
        acols = [
            {"name": "ts", "label": "Time (HKT)", "field": "ts", "sortable": True},
            {"name": "rule", "label": "Rule", "field": "rule", "sortable": True},
            {"name": "action", "label": "Action", "field": "action", "sortable": True},
            {"name": "actor", "label": "Actor", "field": "actor", "sortable": True},
        ]
        arows = [{"ts": ledger.to_hkt(a["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                  "rule": a.get("rule_name", "—"), "action": a["action_taken"],
                  "actor": a.get("actor", "system")} for a in entries]
        ui.table(columns=acols, rows=arows, row_key="ts").classes("w-full").props(
            "dense max-height=240px")

    @ui.refreshable
    def _audit_table() -> None:
        _render_audit(_AUDIT_FILTER["q"])

    def _on_audit_filter(e) -> None:
        _AUDIT_FILTER["q"] = e.value or ""
        _audit_table.refresh()

    # on_change= (not .on("update:model-value")): only the dedicated value-
    # change event carries e.value -- a raw .on() event has .args instead, and
    # reading e.value on it raised, so the filter silently did nothing.
    ui.input(placeholder="Filter rule / action / actor…", on_change=_on_audit_filter) \
        .props("dense outlined clearable").classes("w-64").mark("audit-filter")
    if not audit:
        ui.label("(no audit entries yet)").classes("text-sm text-grey")
    else:
        _audit_table()


@ui.refreshable
def last_refreshed_label() -> None:
    """Header line under the description (layout request 2026-08-16): the
    data-staleness stamp lives with the page identity, not inside the tab
    area. Refreshable so it updates on refresh_all without a page reload.
    Turns amber past 2x the alert-check interval -- silent staleness would
    otherwise read as live data (B2)."""
    if not STATE["last_fetch"]:
        return
    age = (dt.datetime.now(dt.timezone.utc) - STATE["last_fetch"]).total_seconds()
    stale = age > 2 * _ALERT_CHECK_INTERVAL_SEC
    stamp = f"Last refreshed: {ledger.to_hkt(STATE['last_fetch']):%H:%M:%S} (HKT)"
    ui.label(stamp + (" -- stale, retrying…" if stale else "")).classes(
        "text-xs " + ("text-amber-600" if stale else "text-grey-6"))


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

        def _deck_priority(svc: dict) -> tuple:
            st = noc.get_status(svc["name"])
            if not svc.get("monitor"):
                return (5, svc["name"])
            if not st:
                return (4, svc["name"])
            if st.get("locked"):
                return (0, svc["name"])
            if st.get("quarantined"):
                return (1, svc["name"])
            overdue = compliance.get(svc["name"])
            if overdue or st.get("up") is False or (st.get("readiness") == "stale" and svc.get("enforced_cadence")):
                return (2, svc["name"])
            if st.get("muted"):
                return (3, svc["name"])
            return (4, svc["name"])

        for svc in sorted(services.SERVICES, key=_deck_priority):
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
                            dot_color = "bg-grey-5"        # not checked yet
                        elif not up:
                            dot_color = "bg-red-500"        # down
                        elif status and status["readiness"] == "stale":
                            # stale means different things depending on the
                            # agent class: enforced-cadence -> a real fault
                            # (amber); usage-driven (Study Platform) -> just
                            # idle, neutral tone, nothing wrong. Keyed off
                            # enforced_cadence, not restart_on_staleness --
                            # Quant Live is a real fault when stale but must
                            # never auto-restart, so the two fields diverge
                            # for it (ADDED 2026-08-17).
                            dot_color = "bg-amber-500" if svc.get("enforced_cadence") \
                                else "bg-grey-6"
                        else:
                            dot_color = "bg-green-500"      # healthy
                        ui.element("div").classes(
                            f"w-2.5 h-2.5 rounded-full {dot_color} shrink-0")
                    ui.label(svc["name"]).classes("font-bold")
                    _impact_badge(svc.get("business_impact"))
                    if svc["name"] in _CARD_PROJECTS:
                        # Per-project drill-down (A1): jump the cost tabs to
                        # just this agent's ledger rows.
                        ui.button(icon="filter_alt", on_click=lambda p=_CARD_PROJECTS[svc["name"]]: _set_project(p)) \
                            .props("dense flat round size=sm color=grey-6") \
                            .tooltip(f"Show only {svc['name']}'s costs")
                    if status and status.get("locked"):
                        # Phase 5 A1: say WHAT KIND of lock -- auto-unlock
                        # pending vs sticky "needs you" -- so the red button
                        # only demands attention when it truly requires it.
                        info = status.get("lock_info") or {}
                        if info.get("sticky"):
                            ui.label("Locked · needs you (2nd strike)").classes(
                                "text-xs bg-red-100 text-red-700 rounded px-1")
                        else:
                            ui.label(f"Locked · auto-unlock "
                                     f"~{info.get('unlocks_in_min', '?')}m").classes(
                                "text-xs bg-amber-100 text-amber-800 rounded px-1")
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
                        if status.get("muted"):
                            # Phase 5 A3: the card says why it's quiet -- the
                            # NOC is observing, not acting, by operator choice.
                            ui.label("monitoring paused (maintenance)").classes(
                                "text-xs text-grey-6")
                        if status["up"] is False:
                            ui.label("down").classes("text-xs text-red-600")
                        elif status["readiness"] == "stale":
                            if svc.get("enforced_cadence"):
                                ui.label(f"degraded -- {status['readiness_detail'] or 'stale data'}")\
                                    .classes("text-xs text-amber-600")
                            else:
                                ui.label(f"idle -- {status['readiness_detail'] or 'no recent usage'}")\
                                    .classes("text-xs text-grey-6")
                        if status.get("blocked_by"):
                            ui.label("blocked by: " + ", ".join(status["blocked_by"])).classes(
                                "text-xs text-amber-700 bg-amber-50 rounded px-1 mt-1")
                        if status.get("uptime_7d") is not None:
                            # Uptime strip (A4): the trailing week as 28
                            # six-hour slots -- WHEN in the week an agent was
                            # down is information a single percentage loses.
                            with ui.row().classes("w-full items-center gap-2 mt-1"):
                                ui.label(f"7d uptime: {status['uptime_7d']:.1f}%").classes(
                                    "text-xs text-grey-6")
                                slots = noc.uptime_slots(svc["name"])
                                slot_cls = {"ok": "bg-green-500", "mixed": "bg-amber-400",
                                            "fail": "bg-red-500", "none": "bg-grey-3"}
                                with ui.row().classes("items-center gap-[2px]"):
                                    for i, s in enumerate(slots):
                                        hours_ago = (len(slots) - 1 - i) * 6
                                        ui.element("div").classes(
                                            f"w-1.5 h-3 rounded-sm {slot_cls[s]}") \
                                            .tooltip(f"{hours_ago}h-{hours_ago + 6}h ago: {s}")
                        if overdue:
                            ui.label("⚠ compliance overdue: " + ", ".join(overdue)).classes(
                                "text-xs text-red-600 mt-1")
                        auth_locked = noc.is_auth_locked(svc["name"])
                        if auth_locked:
                            # Repeated failed passcode attempts against this
                            # agent (ADDED 2026-08-17) -- outranks the normal
                            # quarantine/resume buttons: no further manual
                            # action attempts until this is cleared.
                            ui.label("passcode locked -- too many failed attempts").classes(
                                "text-xs bg-red-100 text-red-700 rounded px-1 mt-1")
                            ui.button("Clear passcode lock", on_click=lambda s=svc: (
                                noc.clear_auth_lock(s["name"]), services_row.refresh())) \
                                .props("dense flat color=red")
                        elif status.get("quarantined"):
                            reason = status["quarantined"]
                            ui.label("auto-quarantined (paused)" if reason == "compliance-auto"
                                     else "quarantined (paused)").classes(
                                "text-xs bg-red-100 text-red-700 rounded px-1 mt-1")
                            ui.button("Resume", on_click=lambda s=svc: _confirm_resume(s)) \
                                .props("dense flat color=positive")
                        elif svc.get("quarantinable") and svc.get("container"):
                            if noc.passcode_configured():
                                ui.button("Quarantine (pause)",
                                         on_click=lambda s=svc: _confirm_quarantine(s)) \
                                    .props("dense flat color=red")
                            else:
                                # Fails closed (ADDED 2026-08-17): no passcode
                                # configured means the action stays disabled
                                # rather than silently working unauthenticated.
                                ui.label("quarantine disabled -- passcode not configured").classes(
                                    "text-xs text-grey-6 mt-1")
                        if svc["restart"] == "auto_heal" and status.get("locked"):
                            ui.button("Clear lock", on_click=lambda s=svc: (
                                noc.clear_lock(s["name"]), services_row.refresh())) \
                                .props("dense flat color=red")
                        # Phase 5 A4: this agent's own recent incidents, inline
                        # -- deciding "clear the lock or investigate" shouldn't
                        # require scrolling the shared log.
                        agent_incidents = [i for i in noc.get_incidents()
                                           if i["agent"] == svc["name"]][:5]
                        if agent_incidents:
                            with ui.expansion(
                                    f"recent incidents ({len(agent_incidents)})") \
                                    .classes("w-full text-xs").props("dense icon=history"):
                                for i in agent_incidents:
                                    ts = ledger.to_hkt(i["ts"]).strftime("%m-%d %H:%M")
                                    line = f"{ts} · {i['event']}"
                                    if i.get("outcome"):
                                        line += f" · {i['outcome']}"
                                    ui.label(line).classes("text-xs text-grey-7")


_INCIDENT_FILTER = {"q": ""}
_AUDIT_FILTER = {"q": ""}
_INCIDENT_VIEW = {"mode": "table"}  # table | timeline


@ui.refreshable
def _incident_log_table() -> None:
    """The table body alone is refreshable so typing in the filter box (which
    lives OUTSIDE this refreshable) can't destroy the input mid-keystroke --
    the same focus-loss trap as the passcode dialog's services_row.refresh()."""
    q = _INCIDENT_FILTER["q"].lower()
    incidents = [i for i in noc.get_incidents()
                 if not q or q in i["agent"].lower() or q in i["event"].lower()
                 or q in i.get("outcome", "").lower() or q in i.get("detail", "").lower()]
    if not incidents:
        ui.label("(no matching incidents)" if q else "(no incidents logged yet)") \
            .classes("text-sm text-grey")
        return
    if _INCIDENT_VIEW["mode"] == "timeline":
        # Timeline view: vertical line with dots, grouped — scan friendly for solo ops
        with ui.element("div").classes("w-full border-l-2 border-zinc-200 ml-2 pl-4 space-y-2 max-h-[240px] overflow-y-auto"):
            for i in incidents[:30]:
                dot = "bg-red-500" if "locked" in i["event"] else "bg-amber-500" if "restart" in i["event"] else "bg-zinc-400"
                ts = ledger.to_hkt(i["ts"]).strftime("%m-%d %H:%M")
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.element("div").classes(f"w-2 h-2 rounded-full {dot} shrink-0 -ml-[21px] border-2 border-white")
                    ui.label(f"{ts}").classes("text-xs font-mono text-zinc-500 shrink-0")
                    ui.label(f"{i['agent']}").classes("text-xs font-bold shrink-0")
                    ui.label(f"{i['event']} · {i.get('outcome','')}").classes("text-xs text-zinc-700 truncate")
        return
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


def incident_log() -> None:
    with ui.row().classes("w-full items-center justify-between mt-4 flex-wrap gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("Incident log").classes("text-sm font-bold")
            # View toggle: table ↔ timeline
            def _set_view(m: str) -> None:
                _INCIDENT_VIEW["mode"] = m
                _incident_log_table.refresh()
            ui.button("Table", on_click=lambda: _set_view("table")).props(
                f"dense {'unelevated' if _INCIDENT_VIEW['mode']=='table' else 'outline'} size=sm").mark("incident-view-table")
            ui.button("Timeline", on_click=lambda: _set_view("timeline")).props(
                f"dense {'unelevated' if _INCIDENT_VIEW['mode']=='timeline' else 'outline'} size=sm").mark("incident-view-timeline")

        def _apply_filter(e) -> None:
            _INCIDENT_FILTER["q"] = e.value or ""
            _incident_log_table.refresh()

        # on_change= (not .on("update:model-value")): only the dedicated
        # value-change event carries e.value -- a raw .on() event has .args
        # instead, and reading e.value on it raised, so the filter silently
        # did nothing.
        ui.input(placeholder="Filter agent / event / outcome…",
                 value=_INCIDENT_FILTER["q"], on_change=_apply_filter) \
            .props("dense outlined clearable").classes("w-64").mark("incident-filter")
    _incident_log_table()


async def _do_retry() -> None:
    await asyncio.to_thread(fetch_stats)
    refresh_all()


@ui.refreshable
def _data_status() -> None:
    """Error/Retry banner, shown above the tabs -- deliberately its OWN small
    refreshable, separate from the tabs/tab_panels chrome below (FIXED
    2026-08-30). Previously this gate and the tabs lived in the same
    refreshable (the old dashboard_body), so every refresh_all() call --
    including ones with nothing to do with cost data -- tore down and
    rebuilt ui.tabs()/ui.tab_panels() from scratch. That's also what caused
    the active tab to revert to Overview on every refresh: a fresh
    ui.tab_panels(tabs, value=initial_tab) is built from whatever STATE
    happened to hold at that exact moment, and the persistence handler
    turned out not to be reliably reaching the server in time (see
    dashboard_body's tabs.on_value_change below for the actual fix)."""
    if STATE["error"]:
        with ui.row().classes("items-center gap-2"):
            ui.label(f"⚠ {STATE['error']}").classes("text-red-600 font-bold")
            ui.button("Retry", on_click=_do_retry).classes("mt-0")


def dashboard_body() -> None:
    """Tab chrome -- built exactly ONCE per page load, NOT a refreshable
    (FIXED 2026-08-30, see _data_status's docstring for why). Each tab's
    content is its own separately-refreshable function (matching how
    governance_view already worked), so refresh_all() can update content
    without ever tearing down the tabs themselves -- which also means a
    selected tab now has nothing destructive happening to it on refresh,
    rather than relying on state-restoration timing to survive one."""
    _data_status()
    tab_names = ["Overview", "Cost & Usage", "Reliability & Incidents", "Governance"]
    with ui.tabs().classes("w-full") as tabs:
        tab_objs = {}
        for name in tab_names:
            tab_objs[name] = ui.tab(name)
    initial_tab = tab_objs.get(STATE["active_tab"], tab_objs["Overview"])

    def _on_tab_change(e) -> None:
        STATE["active_tab"] = str(e.value)

    tabs.on_value_change(_on_tab_change)
    with ui.tab_panels(tabs, value=initial_tab).classes("w-full"):
        with ui.tab_panel(tab_objs["Overview"]):
            _overview_tab()
        with ui.tab_panel(tab_objs["Cost & Usage"]):
            _cost_tab()
        with ui.tab_panel(tab_objs["Reliability & Incidents"]):
            _reliability_tab()
        with ui.tab_panel(tab_objs["Governance"]):
            governance_view()


@ui.refreshable
def _overview_tab() -> None:
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return
    avg_daily = data["total_cost_usd"] / max(data["range_days"], 1)
    monthly_budget = alerts.effective_monthly_budget()
    budget_is_implied = not alerts.MONTHLY_BUDGET_USD
    projected_monthly = avg_daily * 30
    attribution = ledger.attribution_quality(data)
    prev = STATE.get("prev")

    calls_spark = [d["calls"] for d in data.get("daily_series", [])]
    cost_spark = [d["cost_usd"] for d in data.get("daily_series", [])]

    with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 mt-2"):
        d_calls = _delta_sub(data["total_calls"], prev and prev["calls"])
        d_cost = _delta_sub(data["total_cost_usd"], prev and prev["cost_usd"], lower_is_better=True)
        d_ptok = _delta_sub(data["total_prompt_tokens"], prev and prev["prompt_tokens"])
        d_ctok = _delta_sub(data["total_completion_tokens"], prev and prev["completion_tokens"])
        _kpi("Total calls", f"{data['total_calls']:,}", sub=d_calls and d_calls[0],
             sub_cls=d_calls and d_calls[1], spark=calls_spark)
        _kpi("Total cost", f"${data['total_cost_usd']:.4f}", sub=d_cost and d_cost[0],
             sub_cls=d_cost and d_cost[1], spark=cost_spark)
        _kpi("Prompt tokens", f"{data['total_prompt_tokens']:,}", sub=d_ptok and d_ptok[0],
             sub_cls=d_ptok and d_ptok[1])
        _kpi("Completion tokens", f"{data['total_completion_tokens']:,}",
             sub=d_ctok and d_ctok[0], sub_cls=d_ctok and d_ctok[1])
        _kpi("Projected monthly cost",
             f"${projected_monthly:.2f} / ${monthly_budget:.2f} budget"
             + (" (implied)" if budget_is_implied else ""),
             warn=projected_monthly > monthly_budget, spark=cost_spark)
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
        series = project_series + [
            {"type": "line", "name": "alert threshold",
             "data": [alerts.ALERT_DAILY_COST_USD] * len(dbp["dates"]),
             "lineStyle": {"type": "dashed", "color": "#dc2626", "width": 1}, "symbol": "none"},
        ]
        if data["range_days"] != 1 and data.get("daily_series"):
            # Spike markers (A5): a day costing >2x the trailing 7-day mean
            # gets an amber dot -- deterministic math, no LLM. Hourly view
            # skips it (the baseline is daily).
            totals = {r["date"]: r["cost_usd"] for r in data["daily_series"]}
            spikes = ledger.cost_spike_dates(data["daily_series"])
            if spikes:
                series.append({
                    "type": "scatter", "name": "spike (>2x avg)",
                    "symbolSize": 11, "z": 10,
                    "itemStyle": {"color": "#f59e0b"},
                    "data": [{"value": [dbp["dates"].index(d), totals[d]],
                              "label": {"show": True, "position": "top", "fontSize": 9,
                                        "formatter": f"{m}x avg"}}
                             for d, m in spikes.items() if d in totals],
                })
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": {"top": 0, "textStyle": {"fontSize": 10}},
            "xAxis": {"type": "category", "data": dbp["dates"]},
            "yAxis": {"type": "value", "name": "cost (USD)"},
            "series": series,
            "grid": {"left": 50, "right": 20, "top": 40, "bottom": 30},
        }).classes("w-full h-64")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By provider (chatanywhere vs deepseek fallback in action)").classes("text-sm font-bold")
            _bar_chart(data["by_provider"], "provider")


@ui.refreshable
def _cost_tab() -> None:
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return
    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By model").classes("text-sm font-bold")
            _bar_chart(data["by_model"], "model")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project & environment").classes("text-sm font-bold")
            _bar_chart(data["by_environment"], "project", ["environment"])

    with ui.row().classes("w-full items-center justify-between mt-4 flex-wrap gap-2"):
        ui.label("Model usage by project & call type").classes("text-sm font-bold")
        ui.button("Download CSV", icon="download",
                  on_click=lambda: _download_model_usage_csv(data)).props("flat dense")
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


@ui.refreshable
def _reliability_tab() -> None:
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return
    with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
        ui.label("Slowest call types (avg latency)").classes("text-sm font-bold")
        if ledger.latency_ranking(data["by_call_type"]):
            ui.button("Download CSV", icon="download",
                      on_click=lambda: _download_latency_csv(data)).props("flat dense")
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
    """FIXED 2026-08-30: used to call dashboard_body.refresh(), which tore
    down and rebuilt the tabs/tab_panels chrome itself on every single call
    -- that's both the direct cause of the tab-revert bug and needless DOM
    churn on actions that don't even touch cost data. dashboard_body is no
    longer a refreshable (see its docstring); the three cost/data tabs are
    refreshed directly instead, and Governance is deliberately NOT refreshed
    here -- it reads its own independent cache (governance.py), not
    STATE["data"], and already gets its own explicit .refresh() call from
    the one action that actually changes it (_mark_complied)."""
    alert_banner.refresh()
    noc_banner.refresh()
    maintenance_banner.refresh()
    burn_bar.refresh()
    services_row.refresh()
    project_filter_chip.refresh()
    _incident_log_table.refresh()
    _data_status.refresh()
    _overview_tab.refresh()
    _cost_tab.refresh()
    _reliability_tab.refresh()
    last_refreshed_label.refresh()


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
        await asyncio.to_thread(fetch_stats)  # re-fetches the active window
        _refresh_safely(alert_banner)


async def _services_check_loop() -> None:
    while True:
        await asyncio.to_thread(noc.refresh_health)
        _refresh_safely(noc_banner, maintenance_banner, services_row, _incident_log_table)
        await asyncio.sleep(_SERVICES_CHECK_INTERVAL_SEC)


async def _telegram_command_loop() -> None:
    """Phase 5 A2: long-poll the operator's chat for /unlock /status /mute.
    Its own task -- a hung Telegram call can't stall the health loops (same
    isolation pattern as every other loop here)."""
    while True:
        try:
            await asyncio.to_thread(commands.poll_updates)
        except Exception:                              # noqa: BLE001
            log.exception("telegram command loop failed")
        await asyncio.sleep(1)


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
async def main_page() -> None:
    # Dark mode persists via app.storage.user (server-side, keyed to the
    # browser-id cookie NiceGUI already sets) -- previously the toggle reset
    # to light on every reload. storage.user works after the response is
    # sent, unlike a browser-cookie write which only lands during page build.
    stored_dark = bool(app.storage.user.get("dark_mode", False))
    dark_mode = ui.dark_mode(value=stored_dark)

    def _toggle_dark() -> None:
        dark_mode.value = not dark_mode.value
        app.storage.user["dark_mode"] = dark_mode.value
        dark_toggle.props(f"icon={'light_mode' if dark_mode.value else 'dark_mode'}")

    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4"):
        with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
            ui.label("Command Deck").classes("text-2xl font-bold")
            with ui.row().classes("items-center gap-2"):
                # Initial icon must reflect the RESTORED state -- hardcoding
                # "dark_mode" made the button lie about an already-dark page.
                dark_toggle = ui.button(
                    icon="light_mode" if stored_dark else "dark_mode",
                    on_click=_toggle_dark).props("flat round").mark("dark-toggle")
                ui.button("Refresh", icon="refresh",
                          on_click=_do_retry) \
                    .props("color=primary")
                ui.button("Pause monitoring", icon="build",
                          on_click=lambda: _pause_dialog()).props("dense outline color=grey-8") \
                    .mark("mute-btn")

        burn_bar()

        def _pause_monitoring(minutes: int) -> None:
            noc.set_maintenance(minutes)
            ui.notify(f"Monitoring paused for {minutes} min", type="info")
            refresh_all()

        def _pause_dialog() -> None:
            with ui.dialog() as dialog, ui.card():
                ui.label("Pause NOC monitoring?").classes("font-bold")
                ui.label("Restarts, alerts and uptime recording are suppressed "
                         "(probes keep running). Use before a deploy so bouncing "
                         "containers don't trip the auto-heal machinery."
                         ).classes("text-sm text-grey-7")
                with ui.row().classes("justify-end gap-2 mt-2"):
                    for m in (15, 30, 60):
                        ui.button(f"{m}m", on_click=lambda m_=m: (
                            dialog.close(), _pause_monitoring(m_))) \
                            .props("dense outline").mark(f"mute-{m}")
                    ui.button("Cancel", on_click=dialog.close).props("flat")
            dialog.open()

        ui.label("What my agents cost, and whether they're up -- LLM spend across "
                 "quant, study, and event-radar from the shared usage ledger, plus "
                 "live health, auto-heal, and an incident log for every monitored "
                 "agent.").classes("text-sm text-grey-6")
        last_refreshed_label()  # under the description (layout request 2026-08-16)

        alert_banner()
        noc_banner()
        maintenance_banner()

        async def _set_range(e) -> None:
            STATE["days"] = e.value
            STATE["preset_days"] = e.value
            STATE["custom"] = None
            await asyncio.to_thread(fetch_stats, STATE["days"])
            refresh_all()

        async def _apply_custom() -> None:
            start, end = start_date.value, end_date.value
            if not (start and end):
                ui.notify("Pick both a start and end date", type="warning")
                return
            if start > end:
                ui.notify("Start date must be on or before the end date", type="negative")
                return
            STATE["custom"] = (start, end)
            await asyncio.to_thread(fetch_stats)
            refresh_all()

        async def _clear_custom() -> None:
            start_date.set_value(None)
            end_date.set_value(None)
            STATE["custom"] = None
            STATE["days"] = STATE.get("preset_days", 7)
            await asyncio.to_thread(fetch_stats, STATE["days"])
            refresh_all()

        def _save_settings() -> None:
            alerts.set_daily_threshold(threshold_input.value)
            alerts.set_monthly_budget(budget_input.value)
            ui.notify(f"Alert threshold set to ${threshold_input.value:.2f}/day"
                      + (f", monthly budget ${budget_input.value:.2f}" if budget_input.value
                         else ", monthly budget cleared (implied from threshold)"),
                      type="positive")
            refresh_all()

        services_row()

    # Full-width sticky controls: responsive single flex that reflows by width,
    # not by hard 3 rows — deck scrolls away, this bar stays pinned edge-to-edge.
    # Wide: chip | Range+Custom | Alert+Budget on one line (~48px). Narrow: wraps to 2 lines.
    with ui.element("div").classes("sticky top-0 z-20 bg-white border-b border-zinc-200 shadow-sm w-screen ml-[calc(-50vw+50%)]"):
        with ui.element("div").classes("max-w-[1100px] mx-auto px-4 py-2 flex flex-wrap items-center gap-3 gap-y-2"):
            with ui.element("div").classes("shrink-0"):
                project_filter_chip()
            with ui.element("div").classes("flex flex-wrap items-center gap-2 grow basis-[260px]"):
                ui.label("Range:").classes("text-sm shrink-0")
                ui.toggle({1: "Today", 7: "7d", 30: "30d", 90: "90d"}, value=STATE["days"],
                          on_change=lambda e: (_set_range(e))).props("dense").classes("shrink-0")
                custom_btn = ui.button("Custom", icon="calendar_month").props("dense flat").classes("shrink-0").mark("custom-toggle")

            # Threshold / budget — compact 1-line by default, click to edit (stays 1 line even on mobile)
            def _compact_text() -> str:
                thr = alerts.ALERT_DAILY_COST_USD
                bud = alerts.MONTHLY_BUDGET_USD
                if bud:
                    return f"${thr:.2f}/d · ${bud:.0f}/mo"
                return f"${thr:.2f}/d · ~${alerts.effective_monthly_budget():.0f}/mo"

            with ui.element("div").classes("flex flex-nowrap items-center gap-2 shrink-0 ml-auto min-w-0 overflow-hidden"):
                compact_row = ui.element("div").classes("flex items-center gap-1.5")
                with compact_row:
                    compact_label = ui.label(_compact_text()).classes("text-sm whitespace-nowrap truncate")
                    edit_btn = ui.button(icon="edit").props("flat dense round size=sm").classes("shrink-0").tooltip("Edit thresholds")

                edit_row = ui.element("div").classes("hidden flex flex-nowrap items-center gap-1.5")
                with edit_row:
                    ui.label("$").classes("text-sm shrink-0")
                    threshold_input = ui.number(value=alerts.ALERT_DAILY_COST_USD, min=0, step=0.05,
                                                format="%.2f").props("dense outlined").classes("w-[72px] shrink-0 flex-none") \
                        .mark("threshold-input")
                    ui.label("/d").classes("text-xs text-grey-6 shrink-0")
                    ui.label("· $").classes("text-sm shrink-0")
                    budget_input = ui.number(value=alerts.MONTHLY_BUDGET_USD, min=0, step=5,
                                             format="%.2f").props("dense outlined").classes("w-[80px] shrink-0 flex-none") \
                    .mark("budget-input")
                    ui.label("/mo").classes("text-xs text-grey-6 shrink-0")

                def _toggle_threshold_edit() -> None:
                    if "hidden" in edit_row.classes:
                        edit_row.classes(remove="hidden")
                        compact_row.classes(add="hidden")
                    else:
                        edit_row.classes(add="hidden")
                        compact_row.classes(remove="hidden")

                def _do_save() -> None:
                    _save_settings()
                    compact_label.set_text(_compact_text())
                    edit_row.classes(add="hidden")
                    compact_row.classes(remove="hidden")

                # Enter-to-save in edit mode; Save button commits; edit icon toggles
                threshold_input.on("keydown.enter", _do_save)
                budget_input.on("keydown.enter", _do_save)
                with edit_row:
                    ui.button("Save", on_click=_do_save).props("dense flat").classes("shrink-0")
                    ui.button(icon="close", on_click=_toggle_threshold_edit).props("dense flat round size=sm").classes("shrink-0").tooltip("Cancel")
                edit_btn.on_click(_toggle_threshold_edit)

            # Custom range picker — collapsed by default, expands below the main row (w-full)
            picker_row = ui.element("div").classes("hidden w-full flex flex-wrap items-center gap-2 pt-2 mt-1 border-t border-zinc-100")
            with picker_row:
                ui.label("Custom:").classes("text-sm text-grey-6 shrink-0")

                start_date = ui.input(placeholder="Start YYYY-MM-DD") \
                    .props("dense outlined").classes("w-[150px] shrink-0").mark("range-start")
                with start_date.add_slot("append"):
                    ui.icon("event").classes("cursor-pointer").on("click", lambda: start_menu.open())
                with ui.menu().props("no-parent-event") as start_menu:
                    # q-date calendar — picking a date writes YYYY-MM-DD into the input
                    ui.date(on_change=lambda e: start_date.set_value(e.value or "")).props("minimal mask=YYYY-MM-DD")

                ui.label("→").classes("text-xs text-grey-6 shrink-0")

                end_date = ui.input(placeholder="End YYYY-MM-DD") \
                    .props("dense outlined").classes("w-[150px] shrink-0").mark("range-end")
                with end_date.add_slot("append"):
                    ui.icon("event").classes("cursor-pointer").on("click", lambda: end_menu.open())
                with ui.menu().props("no-parent-event") as end_menu:
                    ui.date(on_change=lambda e: end_date.set_value(e.value or "")).props("minimal mask=YYYY-MM-DD")

                async def _do_apply() -> None:
                    await _apply_custom()
                    if "hidden" not in picker_row.classes:
                        picker_row.classes(add="hidden")
                        custom_btn.props("icon=calendar_month")

                async def _do_cancel() -> None:
                    await _clear_custom()
                    if "hidden" not in picker_row.classes:
                        picker_row.classes(add="hidden")
                        custom_btn.props("icon=calendar_month")

                ui.button("Apply", on_click=_do_apply).props("dense flat").classes("shrink-0").mark("apply-custom")
                ui.button("Cancel", on_click=_do_cancel).props("dense flat").classes("shrink-0").mark("clear-custom")

            def _toggle_custom() -> None:
                if "hidden" in picker_row.classes:
                    picker_row.classes(remove="hidden")
                    custom_btn.props("icon=close")
                else:
                    picker_row.classes(add="hidden")
                    custom_btn.props("icon=calendar_month")

            custom_btn.on_click(_toggle_custom)

    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4 pt-2"):
        dashboard_body()  # stays centered, scrolls under the full-width sticky bar

    # FIXED 2026-08-30: was a direct (blocking) fetch_stats() call -- on the
    # shared event loop, a new client's initial load used to freeze every
    # other already-connected client too, not just itself.
    await asyncio.to_thread(fetch_stats)
    refresh_all()


app.on_startup(lambda: asyncio.create_task(_alert_check_loop()))
app.on_startup(lambda: asyncio.create_task(_services_check_loop()))
app.on_startup(lambda: asyncio.create_task(_risk_ledger_loop()))
app.on_startup(lambda: asyncio.create_task(_compliance_loop()))
app.on_startup(lambda: asyncio.create_task(_telegram_command_loop()))

# storage_secret enables app.storage.user -- used to persist the dark-mode
# toggle across reloads. Any non-empty string; not a user-facing secret.
# The standard NiceGUI guard: lets the render-path smoke test (and any future
# pytest suite) `import app` without starting the server.
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="Command Deck", favicon="💰", port=int(os.environ.get("PORT", "8095")),
           reload=False, show=False, storage_secret=os.environ.get("NICEGUI_STORAGE_SECRET",
                                                                   "command-deck-storage"))
