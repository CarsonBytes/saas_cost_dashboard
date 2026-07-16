"""Standalone cross-project LLM usage dashboard -- ADDED 2026-07-16.

Shows usage against the shared chatanywhere.tech + DeepSeek keys across
quant, study, and event-radar (and any future project that writes to the
same Supabase `llm_calls` table): which models, which projects, which call
types within each project, token spend, cost, and environment (paper/live
for quant).

Reads exclusively from the usage-stats Supabase Edge Function -- see
D:\\adaptive_study_platform\\supabase\\functions\\usage-stats\\index.ts,
which owns the actual aggregation query so this dashboard (and any future
consumer) never re-implements that logic. This app does no writing at all;
it's a pure read-only viewer.

Run:  uv run python app.py      (then open http://localhost:8095)

Env (put in .env, see .env.example):
  SUPABASE_URL               required
  SUPABASE_SERVICE_ROLE_KEY  required -- usage-stats requires a valid
                              Supabase JWT (verify_jwt is ON, not disabled --
                              this endpoint returns real per-project cost/
                              usage data, not something to leave open)
"""
from __future__ import annotations

import datetime as dt
import os

import httpx
from dotenv import load_dotenv
from nicegui import ui

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

STATE: dict = {"data": None, "error": None, "days": 7, "last_fetch": None}

_PROJECT_COLORS = {"quant": "#16a34a", "study": "#2563eb", "events": "#9333ea", "(untagged)": "#6b7280"}
_PROVIDER_COLORS = {"chatanywhere": "#16a34a", "deepseek": "#f59e0b", "anthropic": "#9333ea",
                    "openai": "#2563eb", "(untagged)": "#6b7280"}


def fetch_stats(days: int) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        STATE["error"] = "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set -- see .env.example"
        return
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/functions/v1/usage-stats",
            params={"days": days},
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        STATE["data"] = resp.json()
        STATE["error"] = None
        STATE["last_fetch"] = dt.datetime.now()
    except Exception as e:                          # noqa: BLE001
        STATE["error"] = str(e)


def _kpi(title: str, value: str) -> None:
    with ui.card().classes("min-w-[160px] grow"):
        ui.label(title).classes("text-xs text-grey-6")
        ui.label(value).classes("text-xl font-bold")


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
def dashboard_body() -> None:
    if STATE["error"]:
        ui.label(f"⚠ {STATE['error']}").classes("text-red-600 font-bold")
        ui.button("Retry", on_click=lambda: (fetch_stats(STATE["days"]), dashboard_body.refresh()))
        return
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return

    with ui.row().classes("items-center gap-2"):
        ui.label(f"Last refreshed: {STATE['last_fetch']:%H:%M:%S}" if STATE["last_fetch"] else "")\
            .classes("text-xs text-grey-6")

    with ui.row().classes("w-full flex-wrap gap-3 mt-2"):
        _kpi("Total calls", f"{data['total_calls']:,}")
        _kpi("Total cost", f"${data['total_cost_usd']:.4f}")
        _kpi("Prompt tokens", f"{data['total_prompt_tokens']:,}")
        _kpi("Completion tokens", f"{data['total_completion_tokens']:,}")

    if data["daily_series"]:
        ui.label("Calls per day").classes("text-sm font-bold mt-4")
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": [d["date"] for d in data["daily_series"]]},
            "yAxis": {"type": "value", "name": "calls"},
            "series": [{"type": "line", "data": [d["calls"] for d in data["daily_series"]],
                        "smooth": True, "areaStyle": {}, "lineStyle": {"width": 2}}],
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


@ui.page("/")
def main_page() -> None:
    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("LLM Usage Dashboard").classes("text-2xl font-bold")
            ui.button("Refresh", icon="refresh",
                      on_click=lambda: (fetch_stats(STATE["days"]), dashboard_body.refresh()))\
                .props("color=primary")
        ui.label("Cross-project usage: quant (paper + live) + study + event-radar, "
                 "reading from the shared Supabase llm_calls ledger via the usage-stats "
                 "Edge Function.").classes("text-sm text-grey-6")

        def _set_range(e) -> None:
            STATE["days"] = e.value
            fetch_stats(STATE["days"])
            dashboard_body.refresh()

        with ui.row().classes("items-center gap-2"):
            ui.label("Range:").classes("text-sm")
            ui.toggle({1: "Today", 7: "7d", 30: "30d", 90: "90d"}, value=STATE["days"],
                      on_change=_set_range).props("dense")

        dashboard_body()

    fetch_stats(STATE["days"])
    dashboard_body.refresh()


ui.run(title="LLM Usage Dashboard", port=int(os.environ.get("PORT", "8095")), reload=False, show=False)
