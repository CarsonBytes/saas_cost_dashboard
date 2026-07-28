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

Run:  uv run python app.py      (then open http://localhost:8095)

Env (put in .env, see .env.example):
  SUPABASE_URL               required
  SUPABASE_SERVICE_ROLE_KEY  required
  ALERT_DAILY_COST_USD       optional, default 0.50
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional -- no push if unset
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

from nicegui import app, ui

import alerts
import ledger  # both load .env themselves on import
import services

STATE: dict = {"data": None, "rows": None, "error": None, "days": 7, "last_fetch": None,
               "alert": None}

_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("ALERT_CHECK_INTERVAL_SEC", "900"))
_SERVICES_CHECK_INTERVAL_SEC = int(os.environ.get("SERVICES_CHECK_INTERVAL_SEC", "120"))


def fetch_stats(days: int) -> None:
    try:
        rows = ledger.fetch_rows(days)
        STATE["data"] = ledger.build_stats(rows, days)
        STATE["rows"] = rows
        STATE["error"] = None
        STATE["last_fetch"] = dt.datetime.now()
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
def services_row() -> None:
    ui.label("My Services").classes("text-sm font-bold")
    with ui.row().classes("w-full flex-wrap gap-3"):
        for svc in services.SERVICES:
            status = services.get_status(svc["name"])
            up = status["up"] if status else None
            dot_color = "grey-400" if up is None else ("green-600" if up else "red-600")
            with ui.card().classes("min-w-[200px] grow"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("circle", color=dot_color).classes("text-xs")
                    ui.label(svc["name"]).classes("font-bold")
                ui.label(svc["desc"]).classes("text-xs text-grey-6")
                with ui.row().classes("gap-2 mt-1"):
                    for label, url in svc["links"]:
                        ui.link(label, url, new_tab=True).classes("text-sm")


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
        ui.label(f"Last refreshed: {STATE['last_fetch']:%H:%M:%S}" if STATE["last_fetch"] else "") \
            .classes("text-xs text-grey-6")

    insight = ledger.top_spender_insight(data)
    if insight:
        with ui.row().classes("w-full items-center gap-2 bg-blue-50 border border-blue-200 rounded p-2 mt-2"):
            ui.icon("lightbulb", color="blue-600")
            ui.label(insight).classes("text-sm text-blue-900")

    avg_daily = data["total_cost_usd"] / max(data["range_days"], 1)
    monthly_budget = alerts.ALERT_DAILY_COST_USD * 30
    projected_monthly = avg_daily * 30

    with ui.row().classes("w-full flex-wrap gap-3 mt-2"):
        _kpi("Total calls", f"{data['total_calls']:,}")
        _kpi("Total cost", f"${data['total_cost_usd']:.4f}")
        _kpi("Prompt tokens", f"{data['total_prompt_tokens']:,}")
        _kpi("Completion tokens", f"{data['total_completion_tokens']:,}")
        _kpi("Projected monthly cost", f"${projected_monthly:.2f} / ${monthly_budget:.2f} budget",
             warn=projected_monthly > monthly_budget)

    if data["daily_series"]:
        ui.label("Cost per day").classes("text-sm font-bold mt-4")
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": [d["date"] for d in data["daily_series"]]},
            "yAxis": {"type": "value", "name": "cost (USD)"},
            "series": [
                {"type": "line", "data": [d["cost_usd"] for d in data["daily_series"]],
                 "smooth": True, "areaStyle": {}, "lineStyle": {"width": 2}},
                {"type": "line", "name": "alert threshold",
                 "data": [alerts.ALERT_DAILY_COST_USD] * len(data["daily_series"]),
                 "lineStyle": {"type": "dashed", "color": "#dc2626", "width": 1}, "symbol": "none"},
            ],
            "grid": {"left": 50, "right": 20, "top": 20, "bottom": 30},
        }).classes("w-full h-56")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By provider (chatanywhere vs deepseek fallback in action)").classes("text-sm font-bold")
            _bar_chart(data["by_provider"], "provider")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By model").classes("text-sm font-bold")
            _bar_chart(data["by_model"], "model")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By environment (quant paper/live)").classes("text-sm font-bold")
            _bar_chart(data["by_environment"], "project", ["environment"])

    ui.label("Call types by project").classes("text-sm font-bold mt-4")
    cols = [
        {"name": "project", "label": "Project", "field": "project", "sortable": True},
        {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
        {"name": "calls", "label": "Calls", "field": "calls", "sortable": True},
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "sortable": True},
        {"name": "prompt_tokens", "label": "Prompt tok", "field": "prompt_tokens", "sortable": True},
        {"name": "completion_tokens", "label": "Completion tok", "field": "completion_tokens", "sortable": True},
    ]
    rows = [{**r, "cost_usd": f"{r['cost_usd']:.4f}"} for r in data["by_call_type"]]
    ui.table(columns=cols, rows=rows, row_key="call_type").classes("w-full").props("dense")


def refresh_all() -> None:
    alert_banner.refresh()
    services_row.refresh()
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
        await asyncio.to_thread(services.refresh_statuses)
        services_row.refresh()
        await asyncio.sleep(_SERVICES_CHECK_INTERVAL_SEC)


@ui.page("/")
def main_page() -> None:
    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("LLM Usage Dashboard").classes("text-2xl font-bold")
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

        alert_banner()
        services_row()
        ui.separator().classes("my-2")
        dashboard_body()

    fetch_stats(STATE["days"])
    services.refresh_statuses()
    refresh_all()


app.on_startup(lambda: asyncio.create_task(_alert_check_loop()))
app.on_startup(lambda: asyncio.create_task(_services_check_loop()))

ui.run(title="LLM Usage Dashboard", port=int(os.environ.get("PORT", "8095")), reload=False, show=False)
