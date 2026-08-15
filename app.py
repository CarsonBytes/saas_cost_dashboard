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

from nicegui import app, ui

import alerts
import ledger  # both load .env themselves on import
import noc
import services

STATE: dict = {"data": None, "rows": None, "error": None, "days": 7, "last_fetch": None,
               "alert": None}

_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("ALERT_CHECK_INTERVAL_SEC", "900"))
_SERVICES_CHECK_INTERVAL_SEC = int(os.environ.get("SERVICES_CHECK_INTERVAL_SEC", "120"))

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
        for svc in services.SERVICES:
            status = noc.get_status(svc["name"])
            with ui.card().classes("w-full h-full min-h-[120px]"):
                with ui.row().classes("items-center gap-2"):
                    # The icon is identity, always neutral; the round dot
                    # carries the status -- a small classic health indicator
                    # right next to the name. Unmonitored cards get no dot.
                    ui.icon(svc["icon"], color="grey-600").classes("text-2xl")
                    if svc["monitor"]:
                        up = status["up"] if status else None
                        if up is None:
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
                    if status and status.get("locked"):
                        ui.label("Locked").classes(
                            "text-xs bg-red-100 text-red-700 rounded px-1")
                ui.label(svc["desc"]).classes("text-xs text-grey-6")
                with ui.row().classes("items-center gap-1 mt-1 flex-wrap"):
                    for label, url in svc["links"]:
                        if label == "Private":               # generic, off the label
                            ui.icon("lock", size="12px").classes("text-grey-6")
                        ui.link(label, url, new_tab=True).classes("text-sm")
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
        cost_tab = ui.tab("Cost breakdown")
        reliability_tab = ui.tab("Reliability")
    with ui.tab_panels(tabs, value=overview_tab).classes("w-full"):
        with ui.tab_panel(overview_tab):
            _overview_tab(data)
        with ui.tab_panel(cost_tab):
            _cost_tab(data)
        with ui.tab_panel(reliability_tab):
            _reliability_tab(data)


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

    dbp = data["daily_by_project"]
    if dbp["dates"]:
        ui.label("Cost per day, by project").classes("text-sm font-bold mt-4")
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


async def _alert_check_loop() -> None:
    """Runs regardless of whether a browser tab is open (an app-startup
    background task, not tied to any page/client) so a Telegram push can
    fire even if nobody's looking at the dashboard right now. httpx calls in
    fetch_stats are sync, so run them off the event loop thread."""
    while True:
        await asyncio.sleep(_ALERT_CHECK_INTERVAL_SEC)
        await asyncio.to_thread(fetch_stats, STATE["days"])
        alert_banner.refresh()


async def _services_check_loop() -> None:
    while True:
        await asyncio.to_thread(noc.refresh_health)
        noc_banner.refresh()
        services_row.refresh()
        incident_log.refresh()
        await asyncio.sleep(_SERVICES_CHECK_INTERVAL_SEC)


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
        ui.label("Cross-project usage: quant (paper + live) + study + event-radar, "
                 "reading directly from the shared Supabase llm_calls ledger.").classes("text-sm text-grey-6")

        def _set_range(e) -> None:
            STATE["days"] = e.value
            fetch_stats(STATE["days"])
            refresh_all()

        with ui.row().classes("items-center gap-2"):
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

        alert_banner()
        noc_banner()
        services_row()
        ui.separator().classes("my-2")
        dashboard_body()

    fetch_stats(STATE["days"])
    refresh_all()


app.on_startup(lambda: asyncio.create_task(_alert_check_loop()))
app.on_startup(lambda: asyncio.create_task(_services_check_loop()))

ui.run(title="Command Deck", favicon="💰", port=int(os.environ.get("PORT", "8095")), reload=False, show=False)
