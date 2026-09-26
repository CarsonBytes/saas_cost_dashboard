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
import math
import os
import time

from nicegui import app, run, ui

import httpx

import alerts
import commands  # Telegram control plane (/unlock /status /mute)
import governance  # compliance radar engine (governance/engine.py)
import ledger  # both load .env themselves on import
import noc
import services
import supabase_meter  # self-meter file readout (flush target pinned in ledger.py)
from pathlib import Path as _Path

log = logging.getLogger("command-deck")

STATE: dict = {"data": None, "rows": None, "error": None, "days": 7, "last_fetch": None,
               "alert": None,
               # ADDED 2026-08-26 (Phase 4): per-project drill-down filter
               # ("quant"/"events"/"study"/...; None = all projects), the
               # preceding equal-length window's totals for KPI deltas, and
               # an optional custom HKT date range (start, end) overriding
               # the trailing-days toggle.
               "project": None, "prev": None, "custom": None, "preset_days": 7,
               "active_tab": "Overview", "loading": False, "dirty": set()}

_ALERT_CHECK_INTERVAL_SEC = int(os.environ.get("ALERT_CHECK_INTERVAL_SEC", "900"))
_SERVICES_CHECK_INTERVAL_SEC = int(os.environ.get("SERVICES_CHECK_INTERVAL_SEC", "120"))
_RISK_LEDGER_INTERVAL_SEC = int(os.environ.get("RISK_LEDGER_INTERVAL_SEC", "300"))
_COMPLIANCE_INTERVAL_SEC = int(os.environ.get("COMPLIANCE_INTERVAL_SEC", "1800"))
_COMPLIANCE_RETRY_SEC = 60  # after a transient Supabase failure (not a missing table)
# ADDED 2026-09-13: dashboard.carsonng.com is fully public, no Cloudflare
# Access -- found live via a labeled-call-site diagnostic that main_page()
# was firing every ~20-35s with zero manual navigation, consistent with bot/
# crawler traffic hitting the public page repeatedly. STATE is already one
# shared, module-level cache (not per-client), so gating the page-load fetch
# behind a short TTL is safe by construction: every connecting client reads
# the same cached data, only the first hit within each window pays the real
# Supabase cost. Estimated ~590MB/day from this one pattern before the fix
# (~4,000 loads/day x ~150KB/fetch_stats call), closely matching the
# originally-reported ~5xxMB/day Supabase egress. Manual refresh paths
# (_do_retry, the header Refresh button) call fetch_stats() directly, not
# through main_page(), so they always get real data regardless of this TTL.
_PAGE_LOAD_FETCH_TTL_SEC = int(os.environ.get("PAGE_LOAD_FETCH_TTL_SEC", "60"))
# Self-identified crawlers/scripts never trigger a refetch (they get whatever
# STATE holds; only a cold start with nothing cached fetches for them) -- the
# page is public, and each bot hit past the TTL used to cost live Supabase reads.
_BOT_UA_TOKENS = ("bot", "crawl", "spider", "slurp", "curl", "wget", "python-requests",
                  "httpx", "scrapy", "headless", "preview", "monitor", "uptime", "facebookexternalhit")


def _is_bot_request() -> bool:
    try:
        ua = (ui.context.client.request.headers.get("user-agent") or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return not ua or any(t in ua for t in _BOT_UA_TOKENS)
# FIXED 2026-09-14: the TTL check above main_page()'s fetch call is a classic
# check-then-act race -- `await asyncio.to_thread(fetch_stats)` yields control
# back to the event loop, so several connections arriving close together can
# ALL read STATE["last_fetch"] as stale before any of them finishes writing
# the new value, and all launch their own real fetch_stats() call. Confirmed
# live: two 35-37-call/~2.1MB bursts in this app's own meter log, both right
# around a container restart (STATE resets to last_fetch=None, and every
# reconnecting client races the same check simultaneously). This lock makes
# "only the first hit within each window pays the real cost" actually true --
# a losing connection blocks briefly on the lock, then re-checks staleness
# (now fresh) and skips its own fetch, instead of firing a redundant one.
_page_load_fetch_lock = asyncio.Lock()

_PROJECT_COLORS = {"quant": "#16a34a", "study": "#2563eb", "events": "#9333ea", "spendlens": "#0d9488", "(untagged)": "#6b7280"}

# Agent card -> ledger project tag, for the per-project drill-down (A1):
# which cards' costs are actually visible in the shared ledger. Quant Paper
# and Live share project_tag "quant" (split by environment, not by project),
# and Study Platform writes project="study" even though its freshness signal
# comes from answer_log.
_CARD_PROJECTS = {"Quant Trading (Paper)": "quant", "Quant Trading (Live)": "quant",
                  "Event Radar": "events", "Study Platform": "study",
                  "SpendLens": "spendlens"}


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


# Revisiting a window already loaded in the last minute (toggle 7d -> 30d -> 7d)
# reuses the fetched rows instead of re-paging llm_calls. Keyed by the window
# only: the project filter is applied after the fetch, so it shares entries.
_FETCH_CACHE_SEC = 60
_FETCH_CACHE: dict = {}


def _check_alerts_light() -> None:
    """Daily-threshold check for the background loop: tries the O(1)
    llm_daily_summary first (single row read), falls back to raw today rows
    (2 columns) if the summary table doesn't exist."""
    try:
        cost = ledger.today_cost_from_summary()
        if cost is None:
            cost = ledger.today_cost(ledger.fetch_today_rows())
        STATE["alert"] = alerts.run_check(cost)
    except Exception:  # noqa: BLE001
        log.exception("alert check failed")


def fetch_stats(days: int | None = None, force: bool = False) -> None:
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
            n_days = (dt.date.fromisoformat(custom[1])
                      - dt.date.fromisoformat(custom[0])).days + 1
        else:
            n_days = days
        cache_key = ("custom", custom) if custom else ("days", days)
        hit = _FETCH_CACHE.get(cache_key)
        if hit and not force and time.monotonic() - hit[0] < _FETCH_CACHE_SEC:
            rows, prev_rows = hit[1], hit[2]
        else:
            if custom:
                rows, prev_rows = ledger.fetch_rows_custom(*custom)
            else:
                rows, prev_rows = ledger.fetch_rows_and_previous(days)
            _FETCH_CACHE[cache_key] = (time.monotonic(), rows, prev_rows)
            if len(_FETCH_CACHE) > 12:
                _FETCH_CACHE.pop(min(_FETCH_CACHE, key=lambda k: _FETCH_CACHE[k][0]))
        days = n_days
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


def _bar_chart(rows: list[dict], label_field: str, extra_fields: list[str] = None,
               y_name: str = "calls", value_field: str = "calls") -> None:
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
        "yAxis": {"type": "value", "name": y_name},
        "series": [{"type": "bar", "data": [r.get(value_field, 0) for r in rows],
                    "itemStyle": {"color": "#2563eb"}}],
        "grid": {"left": 50, "right": 20, "top": 20, "bottom": 60},
    }).classes("w-full h-56")


_APP_COLORS = {"dashboard": "#7c3aed", "study": "#2563eb", "study-demo": "#60a5fa",
               "study-native": "#93c5fd", "event-radar": "#9333ea", "event-radar-demo": "#a78bfa",
               "quant": "#16a34a", "quant-paper": "#16a34a", "quant-live": "#dc2626",
               "spendlens": "#0d9488", "unknown": "#6b7280"}
_APP_FALLBACK = ["#f59e0b", "#dc2626", "#db2777", "#0891b2"]


def _app_color(app_name: str, n: int) -> str:
    return _APP_COLORS.get(app_name) or _APP_FALLBACK[n % len(_APP_FALLBACK)]


def _egress_series(rows: list[dict], start: dt.datetime, end: dt.datetime,
                   now: dt.datetime | None = None) -> dict:
    """Bucket meter_rollup rows into HKT hours (windows up to 2 days) or HKT
    days, per app: {"labels", "apps": {app: {"req": [...], "mb": [...]}}}.
    Empty buckets are kept so gaps in traffic show as gaps, not as joins."""
    now = now or dt.datetime.now(dt.timezone.utc)
    hourly = (end - start) <= dt.timedelta(days=2)
    step = dt.timedelta(hours=1) if hourly else dt.timedelta(days=1)
    fmt = "%m-%d %H:00" if hourly else "%m-%d"
    stop = min(end, now + step)
    buckets, t = [], start
    while t < stop:
        buckets.append(t.astimezone(ledger._HKT).strftime(fmt))
        t += step
    index = {b_: i for i, b_ in enumerate(buckets)}
    apps: dict[str, dict] = {}
    for r in rows:
        key = ledger._utc(r["ts"]).astimezone(ledger._HKT).strftime(fmt)
        i = index.get(key)
        if i is None:
            continue
        a_ = apps.setdefault(r.get("app") or "?", {"req": [0] * len(buckets), "mb": [0.0] * len(buckets)})
        a_["req"][i] += r.get("requests") or 0
        a_["mb"][i] += (r.get("bytes") or 0) / 1e6
    for a_ in apps.values():
        a_["mb"] = [round(v, 2) for v in a_["mb"]]
    return {"labels": buckets, "apps": apps}


def _egress_totals(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """{app: (requests, bytes)} over a set of rollup rows."""
    out: dict[str, list[int]] = {}
    for r in rows:
        cell = out.setdefault(r.get("app") or "?", [0, 0])
        cell[0] += r.get("requests") or 0
        cell[1] += r.get("bytes") or 0
    return {k: (v[0], v[1]) for k, v in out.items()}


def _egress_chart(series: dict, field: str, title: str, unit: str, *, height: str = "h-64") -> None:
    """Stacked smooth-area chart (same look as the Overview cost-by-project
    chart) of one field ("req" | "mb") per app over time."""
    order = sorted(series["apps"], key=lambda a_: -sum(series["apps"][a_][field]))
    ui.label(title).classes("text-sm font-bold")
    ui.echart({
        "tooltip": {"trigger": "axis"},
        "legend": {"top": 0, "textStyle": {"fontSize": 10}},
        "xAxis": {"type": "category", "data": series["labels"],
                  "axisLabel": {"fontSize": 10, "hideOverlap": True}},
        "yAxis": {"type": "value", "name": unit},
        "series": [{"type": "line", "name": a_, "stack": "total", "smooth": True,
                    "areaStyle": {}, "lineStyle": {"width": 1}, "symbol": "none",
                    "data": series["apps"][a_][field],
                    "itemStyle": {"color": _app_color(a_, n)}}
                   for n, a_ in enumerate(order)],
        "grid": {"left": 50, "right": 20, "top": 40, "bottom": 30},
    }).classes(f"w-full {height}").mark(f"egress-chart-{field}")


def _pct_delta(cur: float, prev: float) -> str:
    return f"{(cur - prev) / prev:+.0%}" if prev else "-"


def _derived_prompt_stats(by_project: list[dict]) -> list[dict]:
    """Compute per-project derived prompt token stats from by_project aggregate.

    Returns a list of dicts with:
      - project: project name
      - prompt_per_call: avg prompt tokens per LLM call
      - prompt_completion_ratio: prompt_tokens / completion_tokens (>1 = input-heavy)
      - prompt_pct: prompt tokens as % of total tokens
    """
    stats = []
    for r in by_project:
        calls = r.get("calls") or 0
        pt = r.get("prompt_tokens") or 0
        ct = r.get("completion_tokens") or 0
        total = pt + ct
        stats.append({
            "project": r.get("project", "?"),
            "prompt_per_call": round(pt / calls) if calls else 0,
            "prompt_completion_ratio": round(pt / ct, 2) if ct else 0,
            "prompt_pct": round(100 * pt / total, 1) if total else 0,
        })
    return stats


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
    if not governance.cached_loaded():
        ui.label("Loading governance data…").classes("text-sm text-grey")
        return
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
    audit = governance.cached_audit()

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
    if age < 60:
        age_str = "just now"
    elif age < 3600:
        age_str = f"{int(age / 60)}m ago"
    else:
        age_str = f"{int(age / 3600)}h {int((age % 3600) / 60)}m ago"
    stamp = f"Last refreshed: {ledger.to_hkt(STATE['last_fetch']):%H:%M:%S} (HKT) \u2014 {age_str}"
    ui.label(stamp + (" -- stale, retrying\u2026" if stale else "")).classes(
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
                        # ADDED 2026-09-04 (memory-control spec item #4):
                        # visibility only at this stage -- no cap exists yet
                        # to compare against (spec item #1), so no alert
                        # threshold here, just the reading a future cap would
                        # be sized from. None means "not on the restart-
                        # proxy's allow-list" or "no reading yet", not zero.
                        if status.get("memory_mb") is not None:
                            mem_text = f"Memory: {status['memory_mb']:.0f} MB"
                            if status.get("memory_limit_mb"):
                                mem_text += f" / {status['memory_limit_mb']:.0f} MB cap"
                            ui.label(mem_text).classes("text-xs text-grey-6")
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
    rows = [{"_key": n, "ts": ledger.to_hkt(i["ts"]).strftime("%Y-%m-%d %H:%M:%S"), "agent": i["agent"],
             "event": i["event"], "outcome": i.get("outcome", ""),
             "detail": i.get("detail", "")} for n, i in enumerate(incidents)]
    ui.table(columns=cols, rows=rows, row_key="_key").classes("w-full").props(
        "dense max-height=240px")


def incident_log() -> None:
    with ui.row().classes("w-full items-center justify-between mt-4 flex-wrap gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("Incident log").classes("text-sm font-bold")
            # View toggle: table <-> timeline. A ui.toggle owns its selected
            # state; the old pair of buttons baked 'unelevated'/'outline' into
            # their props at build time, so the highlight never followed clicks.
            def _set_view(e) -> None:
                _INCIDENT_VIEW["mode"] = e.value
                _incident_log_table.refresh()
            ui.toggle({"table": "Table", "timeline": "Timeline"}, value=_INCIDENT_VIEW["mode"],
                      on_change=_set_view).props("dense no-caps size=sm").mark("incident-view-toggle")

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
    await asyncio.to_thread(fetch_stats, None, True)
    if STATE.get("active_tab") in ("Supabase", "Overview"):
        await asyncio.to_thread(_prewarm_supabase)
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
    tab_names = ["Overview", "Supabase", "Cost & Usage", "Reliability & Incidents", "Governance", "Access Log"]
    with ui.tabs().classes("w-full") as tabs:
        tab_objs = {}
        for name in tab_names:
            tab_objs[name] = ui.tab(name)
    initial_tab = tab_objs.get(STATE["active_tab"], tab_objs["Overview"])
    # LAZY TABS: only the selected tab's content is built/fetched. Others are
    # built on first visit, and rebuilt on a later visit only if data changed
    # while they were hidden (STATE["dirty"], see refresh_all()).
    built: set[str] = set()
    panels: dict[str, ui.element] = {}

    async def _show_tab(name: str) -> None:
        fn = _TAB_BUILDERS.get(name)
        if fn is None:
            return
        if name not in built:
            if name in ("Supabase", "Overview"):
                STATE["loading"] = True
                try:
                    await asyncio.to_thread(_prewarm_supabase)
                finally:
                    STATE["loading"] = False
            with panels[name]:
                fn()
            built.add(name)
            STATE["dirty"].discard(name)
        elif name in STATE["dirty"]:
            STATE["dirty"].discard(name)
            if name in ("Supabase", "Overview"):
                STATE["loading"] = True
                try:
                    await asyncio.to_thread(_prewarm_supabase)
                finally:
                    STATE["loading"] = False
            fn.refresh()

    async def _on_tab_change(e) -> None:
        STATE["active_tab"] = str(e.value)
        await _show_tab(STATE["active_tab"])

    tabs.on_value_change(_on_tab_change)
    with ui.tab_panels(tabs, value=initial_tab).classes("w-full"):
        for name in tab_names:
            with ui.tab_panel(tab_objs[name]) as panel:
                panels[name] = panel
                if name == "Governance":
                    governance_view()
                elif name == STATE["active_tab"] or (name == "Overview" and STATE["active_tab"] not in tab_names):
                    _TAB_BUILDERS[name]()
                    built.add(name)


def _overview_egress() -> dict | None:
    """Brief Supabase egress summary for the Overview cards/chart, over the
    same window as the rest of the tab. Reads the hourly-stored rollup (no
    extra requests once warm); None when there is no metered data."""
    try:
        start, end, _label = _supabase_window()
        rows = ledger.fetch_meter_rollup(since_iso=start.isoformat(), until_iso=end.isoformat())
        if not rows:
            return None
        prev_rows = ledger.fetch_meter_rollup(since_iso=(start - (end - start)).isoformat(),
                                              until_iso=start.isoformat())
        series = _egress_series(rows, start, end)
        req = sum(r.get("requests") or 0 for r in rows)
        byts = sum(r.get("bytes") or 0 for r in rows)
        prev_req = sum(r.get("requests") or 0 for r in prev_rows)
        prev_b = sum(r.get("bytes") or 0 for r in prev_rows)
        tot = lambda f: [sum(v[i] for v in (a_[f] for a_ in series["apps"].values()))  # noqa: E731
                         for i in range(len(series["labels"]))]
        return {"req": req, "mb": byts / 1e6, "series": series,
                "d_req": _delta_sub(req, prev_req, lower_is_better=True),
                "d_mb": _delta_sub(byts, prev_b, lower_is_better=True),
                "spark_req": tot("req"), "spark_mb": tot("mb")}
    except Exception:  # noqa: BLE001
        log.exception("overview egress summary failed")
        return None


@ui.refreshable
def _overview_tab() -> None:
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return
    prev = STATE.get("prev")

    calls_spark = [d["calls"] for d in data.get("daily_series", [])]
    cost_spark = [d["cost_usd"] for d in data.get("daily_series", [])]
    ptok_spark = [d["prompt_tokens"] for d in data.get("daily_series", [])]
    ctok_spark = [d["completion_tokens"] for d in data.get("daily_series", [])]

    eg = _overview_egress()
    with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 mt-2"):
        d_calls = _delta_sub(data["total_calls"], prev and prev["calls"])
        d_cost = _delta_sub(data["total_cost_usd"], prev and prev["cost_usd"], lower_is_better=True)
        d_ptok = _delta_sub(data["total_prompt_tokens"], prev and prev["prompt_tokens"])
        d_ctok = _delta_sub(data["total_completion_tokens"], prev and prev["completion_tokens"])
        _kpi("Total cost", f"${data['total_cost_usd']:.4f}", sub=d_cost and d_cost[0],
             sub_cls=d_cost and d_cost[1], spark=cost_spark)
        _kpi("LLM calls", f"{data['total_calls']:,}", sub=d_calls and d_calls[0],
             sub_cls=d_calls and d_calls[1], spark=calls_spark)
        _kpi("Prompt tokens", f"{data['total_prompt_tokens']:,}", sub=d_ptok and d_ptok[0],
             sub_cls=d_ptok and d_ptok[1], spark=ptok_spark)
        _kpi("Completion tokens", f"{data['total_completion_tokens']:,}",
             sub=d_ctok and d_ctok[0], sub_cls=d_ctok and d_ctok[1], spark=ctok_spark)
        if eg:
            _kpi("Supabase requests", f"{eg['req']:,}", sub=eg["d_req"] and eg["d_req"][0],
                 sub_cls=eg["d_req"] and eg["d_req"][1], spark=eg["spark_req"])
            _kpi("Supabase egress (metered)", f"{eg['mb']:.1f} MB", sub=eg["d_mb"] and eg["d_mb"][0],
                 sub_cls=eg["d_mb"] and eg["d_mb"][1], spark=eg["spark_mb"])

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

    eg_series = eg["series"] if eg else None
    if eg_series and eg_series["apps"]:
        with ui.column().classes("w-full mt-4 gap-1"):
            _egress_chart(eg_series, "mb", "Supabase egress by app (MB, metered) -- details in the Supabase tab",
                          "MB", height="h-44")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("By project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project", y_name="LLM calls")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Prompt tokens by project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project", y_name="Prompt tokens", value_field="prompt_tokens")


@ui.refreshable
def _cost_tab() -> None:
    data = STATE["data"]
    if not data:
        ui.label("Loading…").classes("text-sm text-grey")
        return
    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("LLM calls by model").classes("text-sm font-bold")
            _bar_chart(data["by_model"], "model", y_name="LLM calls")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("LLM calls by project & environment").classes("text-sm font-bold")
            _bar_chart(data["by_environment"], "project", ["environment"], y_name="LLM calls")

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Prompt tokens by project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project", y_name="Prompt tokens", value_field="prompt_tokens")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Completion tokens by project").classes("text-sm font-bold")
            _bar_chart(data["by_project"], "project", y_name="Completion tokens", value_field="completion_tokens")

    prompt_stats = _derived_prompt_stats(data["by_project"])
    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Prompt tokens per LLM call (by project)").classes("text-sm font-bold")
            _bar_chart(prompt_stats, "project", y_name="Avg prompt tokens/call", value_field="prompt_per_call")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Prompt-to-completion ratio (by project)").classes("text-sm font-bold")
            _bar_chart(prompt_stats, "project", y_name="Ratio (>1 = input-heavy)", value_field="prompt_completion_ratio")
        with ui.column().classes("grow min-w-[300px]"):
            ui.label("Prompt tokens as % of total (by project)").classes("text-sm font-bold")
            _bar_chart(prompt_stats, "project", y_name="% prompt tokens", value_field="prompt_pct")

    with ui.row().classes("w-full items-center justify-between mt-4 flex-wrap gap-2"):
        ui.label("LLM model usage by project & call type").classes("text-sm font-bold")
        ui.button("Download CSV", icon="download",
                  on_click=lambda: _download_model_usage_csv(data)).props("flat dense")
    model_cols = [
        {"name": "project", "label": "Project", "field": "project", "sortable": True},
        {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
        {"name": "model", "label": "Model", "field": "model", "sortable": True},
        {"name": "calls", "label": "LLM calls", "field": "calls", "sortable": True},
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
        ui.label("LLM call types by project").classes("text-sm font-bold")
        ui.button("Download CSV", icon="download", on_click=lambda: _download_call_types_csv(data)) \
            .props("flat dense")
    cols = [
        {"name": "project", "label": "Project", "field": "project", "sortable": True},
        {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
        {"name": "calls", "label": "LLM calls", "field": "calls", "sortable": True},
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "sortable": True},
        {"name": "cost_per_call", "label": "$/LLM call", "field": "cost_per_call", "sortable": True},
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
        ui.label("Slowest LLM call types (avg latency)").classes("text-sm font-bold")
        if ledger.latency_ranking(data["by_call_type"]):
            ui.button("Download CSV", icon="download",
                      on_click=lambda: _download_latency_csv(data)).props("flat dense")
    latency_ranked = ledger.latency_ranking(data["by_call_type"])
    if latency_ranked:
        lat_cols = [
            {"name": "project", "label": "Project", "field": "project", "sortable": True},
            {"name": "call_type", "label": "Call type", "field": "call_type", "sortable": True},
            {"name": "avg_latency_ms", "label": "Avg latency (ms)", "field": "avg_latency_ms", "sortable": True},
            {"name": "calls", "label": "LLM calls", "field": "calls", "sortable": True},
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

    # Self-meter (2026-09-13): this dashboard process's own Supabase REST calls
    # since the last minute-flush -- the per-app attribution that Supabase's
    # 1-hour edge_logs window can't give. Full history: state/supabase_meter.jsonl.
    meter = ledger.supabase_meter_snapshot()
    meter_line = ", ".join(f"{k}: {v}" for k, v in sorted(meter.items())) or "no calls yet this minute"
    ui.label(f"Dashboard self-meter (this minute): {meter_line}").classes("text-xs text-grey-6 mt-4") \
        .mark("self-meter")


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _supabase_window() -> tuple[dt.datetime, dt.datetime, str]:
    """Effective (start, end, label) of the Supabase tab's window from STATE
    (same pattern as fetch_stats)."""
    custom = STATE.get("custom")
    if custom:
        return (ledger._hkt_day_start_utc(custom[0]),
                ledger._hkt_day_start_utc(custom[1]) + dt.timedelta(days=1),
                f"{custom[0]} to {custom[1]}")
    n_days = STATE.get("days", 7)
    end = ledger._hkt_today_start_utc() + dt.timedelta(days=1)
    return end - dt.timedelta(days=n_days), end, f"{n_days}d"


@ui.refreshable
def _supabase_tab() -> None:
    """Supabase egress monitor (2026-09-13): reconciliation, not just
    attribution. Section 1 compares Supabase's own reported totals against
    what the self-meters see (the gap IS the finding -- one app's file can
    never match the project bill). Section 2 breaks the metered traffic down
    per app x endpoint from the shared meter_rollup table. Section 3 is this
    dashboard's own per-endpoint detail from its local file."""
    import supabase_usage
    path = _Path(__file__).parent / "state" / "supabase_meter.jsonl"

    _start_dt, _end_dt, window_label = _supabase_window()
    window_start_ts = _start_dt.timestamp()
    window_start_iso = _start_dt.isoformat()
    window_end_iso = _end_dt.isoformat()

    # ---- 1. reconciliation -------------------------------------------------
    # Management API only supports predefined interval buckets (1day, 3day,
    # 7day), not arbitrary date ranges. Select the coarsest bucket that covers
    # the user's window so the reported figure is comparable to the metered.
    # The API's buckets are always trailing from NOW, so the interval must
    # reach back to the window's start (not just match its length), and only
    # buckets inside [start, end) may be summed. It never covers more than
    # ~7 days, so reconcile only over the span it can actually see.
    days_back = max(math.ceil((dt.datetime.now(dt.timezone.utc) - _start_dt).total_seconds() / 86400), 1)
    mgmt_interval = supabase_usage.best_interval_for_days(days_back)
    ui.label(f"Reported vs metered ({window_label})").classes("text-sm font-bold")
    usage = supabase_usage.fetch_reported_usage(interval=mgmt_interval)
    _rep_first = supabase_usage.reported_first_ts(usage)
    rec_start = max(_start_dt, _rep_first) if _rep_first else _start_dt
    reported, note = supabase_usage.reported_requests(usage, since=rec_start, until=_end_dt)
    rec_rows = ledger.fetch_meter_rollup(since_iso=rec_start.isoformat(), until_iso=window_end_iso)
    rec_req = sum(r.get("requests") or 0 for r in rec_rows)
    rec_bytes = sum(r.get("bytes") or 0 for r in rec_rows)
    rollup_rows = ledger.fetch_meter_rollup(since_iso=window_start_iso, until_iso=window_end_iso)
    rollup_apps = {r.get("app") for r in rollup_rows}
    rollup_req = sum(r.get("requests") or 0 for r in rollup_rows)
    rollup_bytes = sum(r.get("bytes") or 0 for r in rollup_rows)
    file_counts = supabase_meter.read_totals(str(path), since_ts=window_start_ts)
    file_bytes = supabase_meter.read_bytes(str(path), since_ts=window_start_ts)
    # The dashboard appears in BOTH sources once its writer posts to the
    # rollup table -- count its file only while it's absent there.
    _rec_has_self = supabase_meter.APP in {r.get("app") for r in rec_rows}
    metered_req = rec_req + (0 if _rec_has_self else sum(file_counts.values()))
    metered_bytes = rec_bytes + (0 if _rec_has_self else sum(file_bytes.values()))
    if reported:
        coverage = metered_req / reported if reported else 0
        est = f"~{_fmt_bytes(int(metered_bytes / coverage))}" if coverage > 0 else "-"
        span = ("" if rec_start <= _start_dt else
                f" -- Supabase's API only covers the last {days_back if days_back <= 7 else 7}d, "
                f"so reconciled over {ledger.to_hkt(rec_start):%Y-%m-%d} onward")
        ui.label(f"Supabase reports {reported:,} requests/{window_label} ({note}){span}. "
                 f"Self-meters see {metered_req:,} ({coverage:.0%} coverage). "
                 f"Metered payload: {_fmt_bytes(metered_bytes)} -> estimated true "
                 f"egress {est} (assumes unmetered traffic has the same "
                 f"bytes/request; headers/TLS/compression all live in the gap).")             .classes("text-sm mt-1").mark("reconciliation-line")
    else:
        ui.label(f"Supabase-reported total unavailable: {note}. Metered traffic "
                 f"below is this ecosystem's own count only.") \
            .classes("text-sm text-grey-6 mt-1").mark("reconciliation-line")

    # ---- 1b. egress over time by app, with change vs the previous window -----
    ui.label(f"Egress over time by app ({window_label})").classes("text-sm font-bold mt-4")
    series = _egress_series(rollup_rows, _start_dt, _end_dt)
    if series["apps"]:
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.column().classes("grow min-w-[320px]"):
                _egress_chart(series, "req", "HTTP requests", "requests")
            with ui.column().classes("grow min-w-[320px]"):
                _egress_chart(series, "mb", "Metered egress (MB)", "MB")
        length = _end_dt - _start_dt
        prev_rows = ledger.fetch_meter_rollup(since_iso=(_start_dt - length).isoformat(),
                                              until_iso=_start_dt.isoformat())
        cur_t, prev_t = _egress_totals(rollup_rows), _egress_totals(prev_rows)
        ui.label(f"Change vs the previous {window_label if not STATE.get('custom') else 'equal-length window'}"
                 ).classes("text-sm font-bold mt-2")
        cmp_cols = [
            {"name": "app", "label": "App", "field": "app", "sortable": True},
            {"name": "req", "label": "HTTP req", "field": "req", "sortable": True},
            {"name": "preq", "label": "Prev", "field": "preq"},
            {"name": "dreq", "label": "Change", "field": "dreq"},
            {"name": "mb", "label": "MB", "field": "mb", "sortable": True},
            {"name": "pmb", "label": "Prev MB", "field": "pmb"},
            {"name": "dmb", "label": "Change", "field": "dmb"},
        ]
        cmp_rows = []
        for app_name in sorted(set(cur_t) | set(prev_t), key=lambda k: -cur_t.get(k, (0, 0))[1]):
            (cr, cb), (pr, pb) = cur_t.get(app_name, (0, 0)), prev_t.get(app_name, (0, 0))
            cmp_rows.append({"app": app_name, "req": cr, "preq": pr, "dreq": _pct_delta(cr, pr),
                             "mb": round(cb / 1e6, 1), "pmb": round(pb / 1e6, 1),
                             "dmb": _pct_delta(cb, pb)})
        ui.table(columns=cmp_cols, rows=cmp_rows, row_key="app").classes("w-full").props("dense") \
            .mark("egress-compare-table")
    else:
        ui.label("(no metered traffic in this window)").classes("text-sm text-grey")

    # ---- 1c. unlabeled traffic (spec 2026-09-20) ------------------------------
    # Rendered only when `unknown`-app rows exist in-window. Totals always
    # come from the rollup output; host/pid/argv0 attribution comes from
    # `details` (writers attach them post-005; settled pre-005 rows have
    # none and are tagged unattributable rather than dropped).
    _unknown_rows = [r for r in rollup_rows if (r.get("app") or "?") == "unknown"]
    if _unknown_rows:
        _u_req = sum(r.get("requests") or 0 for r in _unknown_rows)
        _u_bytes = sum(r.get("bytes") or 0 for r in _unknown_rows)
        _u_eps: dict[str, list] = {}
        _u_srcs: dict[tuple, list] = {}
        for r in _unknown_rows:
            _cell = _u_eps.setdefault(r.get("endpoint") or "?", [0, 0])
            _cell[0] += r.get("requests") or 0
            _cell[1] += r.get("bytes") or 0
            for d in r.get("details") or []:
                _s = _u_srcs.setdefault(
                    (d.get("host") or "?", d.get("argv0") or "?", d.get("pid") or "?"), [0, 0])
                _s[0] += r.get("requests") or 0
                _s[1] += r.get("bytes") or 0
        with ui.card().classes("w-full mt-4 border-amber-300").mark("unlabeled-card"):
            ui.label(f"Unlabeled traffic: {_u_req:,} requests, {_fmt_bytes(_u_bytes)} "
                     f"({window_label}) -- no SUPABASE_METER_APP on the sending process(es). "
                     f"See docs/meter-labels.md for the registry and fix.").classes("text-sm font-bold")
            ui.table(columns=[{"name": "ep", "label": "Endpoint", "field": "ep"},
                              {"name": "req", "label": "HTTP req", "field": "req"},
                              {"name": "mb", "label": "MB", "field": "mb"}],
                     rows=[{"ep": e, "req": n, "mb": round(b / 1e6, 2)}
                           for e, (n, b) in sorted(_u_eps.items(), key=lambda kv: -kv[1][0])],
                     row_key="ep").classes("w-full").props("dense")
            if _u_srcs:
                ui.label("Self-identified senders (host / process / pid):").classes("text-xs mt-2")
                ui.table(columns=[{"name": "host", "label": "Host", "field": "host"},
                                  {"name": "proc", "label": "Process", "field": "proc"},
                                  {"name": "pid", "label": "PID", "field": "pid"}],
                         rows=[{"host": h, "proc": p, "pid": pid}
                               for (h, p, pid) in sorted(_u_srcs)],
                         row_key="pid").classes("w-full").props("dense")
            else:
                ui.label("Senders unattributable (pre-005 rows or settled history without "
                         "fingerprints) -- run migrations/005_meter_rollup_detail.sql and "
                         "re-emit to identify them.").classes("text-xs text-grey-6 mt-2")

    # ---- 2. cross-project rollup -------------------------------------------
    ui.label(f"Metered traffic by app x endpoint ({window_label})").classes("text-sm font-bold mt-4")
    if rollup_rows:
        agg: dict[tuple, list] = {}
        for r in rollup_rows:
            key = (r.get("app") or "?", r.get("endpoint") or "?")
            cell = agg.setdefault(key, [0, 0])
            cell[0] += r.get("requests") or 0
            cell[1] += r.get("bytes") or 0
        # --- bar chart: aggregate by app for visual comparison ---
        app_agg: dict[str, int] = {}
        app_bytes: dict[str, int] = {}
        for (a, _e), (n, b) in agg.items():
            app_agg[a] = app_agg.get(a, 0) + n
            app_bytes[a] = app_bytes.get(a, 0) + b
        if app_agg:
            chart_rows = [{"app": a, "calls": n} for a, n in
                          sorted(app_agg.items(), key=lambda kv: -kv[1])]
            _bar_chart(chart_rows, "app", y_name="HTTP req")
        # --- detail table ---
        xcols = [
            {"name": "app", "label": "App", "field": "app", "sortable": True},
            {"name": "endpoint", "label": "Endpoint", "field": "endpoint", "sortable": True},
            {"name": "req", "label": "HTTP req", "field": "req", "sortable": True},
            {"name": "bytes", "label": "Bytes", "field": "bytes"},
            {"name": "avg", "label": "Avg / HTTP req", "field": "avg"},
        ]
        xrows = [{
            "app": a, "endpoint": e, "req": n,
            "bytes": _fmt_bytes(b), "avg": _fmt_bytes(b // max(n, 1)) if n else "—",
            "_key": f"{a} {e}",
        } for (a, e), (n, b) in sorted(agg.items(), key=lambda kv: -kv[1][1])]
        ui.table(columns=xcols, rows=xrows, row_key="_key").classes("w-full").props("dense") \
            .mark("rollup-table")
    else:
        ui.label("No rollup rows yet -- needs migration 004 run in the SQL editor "
                 "AND the sibling apps redeployed with their meter writers "
                 "(each posts ~1 row/min/endpoint).").classes("text-sm text-grey-6")

    # ---- 3. per-endpoint, all apps -------------------------------------------
    # Was this dashboard's OWN local file only (a handful of endpoints), which
    # read as if Supabase traffic were 5 tables while section 2 showed every
    # app's. Now the same window's rollup rows, regrouped by endpoint alone,
    # with the apps behind each one.
    ui.label(f"Supabase HTTP requests by endpoint, all apps ({window_label})").classes("text-sm font-bold mt-4")
    ui.label("Same data as the table above, summed per endpoint (METHOD + table) across every "
             "app. Sizes = response payload bytes (what counts toward egress).")         .classes("text-xs text-grey-6")
    by_ep: dict[str, dict] = {}
    for r in rollup_rows:
        e = by_ep.setdefault(r.get("endpoint") or "?", {"n": 0, "b": 0, "apps": {}})
        e["n"] += r.get("requests") or 0
        e["b"] += r.get("bytes") or 0
        e["apps"][r.get("app") or "?"] = e["apps"].get(r.get("app") or "?", 0) + (r.get("requests") or 0)
    if by_ep:
        cols = [
            {"name": "endpoint", "label": "Endpoint", "field": "endpoint", "sortable": True},
            {"name": "req", "label": f"HTTP req ({window_label})", "field": "req", "sortable": True},
            {"name": "bytes", "label": f"Bytes ({window_label})", "field": "bytes", "sortable": True},
            {"name": "avg", "label": "Avg / HTTP req", "field": "avg"},
            {"name": "apps", "label": "Apps", "field": "apps"},
        ]
        rows = [{
            "endpoint": k, "req": v["n"], "bytes": v["b"], "avg": _fmt_bytes(v["b"] // max(v["n"], 1)),
            "apps": ", ".join(f"{a} ({n:,})" for a, n in sorted(v["apps"].items(), key=lambda kv: -kv[1])),
            "_key": k,
        } for k, v in sorted(by_ep.items(), key=lambda kv: -kv[1]["b"])]
        ui.table(columns=cols, rows=rows, row_key="_key").classes("w-full").props("dense")             .mark("supabase-meter-table")
        ui.label(f"{window_label}: {sum(v['n'] for v in by_ep.values()):,} HTTP req, "
                 f"{_fmt_bytes(sum(v['b'] for v in by_ep.values()))} across {len(by_ep)} endpoints "
                 f"· source: meter_rollup")             .classes("text-xs text-grey-6 mt-2")
    else:
        ui.label("No meter data in this window yet.").classes("text-sm text-grey-6 mt-2")


@ui.refreshable
@ui.refreshable
def _access_log_tab() -> None:
    """Access log tab: shows page-load tracking data from the access_log
    table (IP, region, user agent, and timestamp data).  Reads from the access_log Supabase table
    (see db/access_log.sql)."""
    import access_log
    exclude_localhost = STATE.get("access_log_exclude_localhost", True)
    exclude_bots = STATE.get("access_log_exclude_bots", True)
    try:
        stats = access_log.fetch_access_stats(days=7, exclude_localhost=exclude_localhost, exclude_bots=exclude_bots)
    except Exception:  # noqa: BLE001
        ui.label("Failed to load access log data. Make sure the access_log "
                 "table exists (run db/access_log.sql in Supabase SQL editor)."
                 ).classes("text-sm text-red-600")
        return

    # Toggle for excluding localhost
    def _on_toggle(e):
        STATE["access_log_exclude_localhost"] = e.value
        _access_log_tab.refresh()

    def _on_bot_toggle(e):
        STATE["access_log_exclude_bots"] = e.value
        _access_log_tab.refresh()

    with ui.row().classes("gap-4 mt-2"):
        ui.switch("Exclude localhost (127.0.0.1)", value=exclude_localhost,
                  on_change=_on_toggle).classes("text-sm")
        ui.switch("Exclude bots", value=exclude_bots,
                  on_change=_on_bot_toggle).classes("text-sm")

    # Charts first — most meaningful at a glance
    hourly = stats.get("hourly", {})
    regions = stats.get("regions", {})
    top_paths = stats.get("top_paths", [])

    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        # Hourly trend
        if hourly:
            with ui.column().classes("grow min-w-[400px]"):
                ui.label("Visits over time (7d, hourly)").classes("text-sm font-bold")
                hours = sorted(hourly.keys())
                human_data = [hourly[h]["human"] for h in hours]
                bot_data = [hourly[h]["bot"] for h in hours]
                ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "legend": {"data": ["Human", "Bot"]},
                    "xAxis": {"type": "category", "data": [h[11:13] + ":00" for h in hours],
                              "axisLabel": {"fontSize": 10, "rotate": 45}},
                    "yAxis": {"type": "value", "name": "visits"},
                    "series": [
                        {"name": "Human", "type": "bar", "stack": "total",
                         "data": human_data, "itemStyle": {"color": "#2563eb"}},
                        {"name": "Bot", "type": "bar", "stack": "total",
                         "data": bot_data, "itemStyle": {"color": "#9ca3af"}},
                    ],
                    "grid": {"left": 50, "right": 20, "top": 30, "bottom": 60},
                }).classes("w-full h-56")

        # Region breakdown
        if regions:
            with ui.column().classes("grow min-w-[300px]"):
                ui.label("Visits by region (human only)").classes("text-sm font-bold")
                region_rows = [{"region": r, "count": c}
                               for r, c in sorted(regions.items(), key=lambda kv: -kv[1])]
                _bar_chart(region_rows, "region", y_name="visits")

    # Top paths
    if top_paths:
        ui.label("Most visited paths (human only)").classes("text-sm font-bold mt-4")
        path_rows = [{"path": p, "visits": c} for p, c in top_paths]
        _bar_chart(path_rows, "path", y_name="visits")

    # KPI cards
    with ui.row().classes("w-full gap-4 mt-4 flex-wrap"):
        _kpi("Total visits (7d)", f"{stats['total']:,}")
        _kpi("Unique IPs (7d)", f"{stats['unique_ips']:,}")
        _kpi("Human visits (7d)", f"{stats['humans']:,}")
        _kpi("Bot hits (7d)", f"{stats['bots']:,}")
        _kpi("Avg visits / human IP", f"{stats['avg_visits_per_ip']:.1f}")

    # Recent entries
    recent = stats.get("recent", [])
    if recent:
        ui.label(f"Recent access entries (last {len(recent)})").classes("text-sm font-bold mt-4")
        cols = [
            {"name": "ts", "label": "Time (HKT)", "field": "ts", "sortable": True},
            {"name": "ip", "label": "IP", "field": "ip"},
            {"name": "region", "label": "Region", "field": "region", "sortable": True},
            {"name": "path", "label": "Path", "field": "path"},
            {"name": "user_agent", "label": "User Agent", "field": "user_agent"},
            {"name": "is_bot", "label": "Bot?", "field": "is_bot"},
        ]
        rows = []
        for r in recent:
            try:
                ts_str = r.get("ts", "")
                if ts_str:
                    ts_dt = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts_hkt = ledger.to_hkt(ts_dt).strftime("%m-%d %H:%M:%S")
                else:
                    ts_hkt = "?"
            except Exception:  # noqa: BLE001
                ts_hkt = ts_str[:16] if ts_str else "?"
            rows.append({
                "ts": ts_hkt,
                "ip": r.get("ip", "?"),
                "region": r.get("region", "?"),
                "path": r.get("path", "?"),
                "user_agent": (r.get("user_agent") or "?")[:80],
                "is_bot": "Yes" if r.get("is_bot") else "No",
            })
        ui.table(columns=cols, rows=rows, row_key="ts").classes("w-full").props("dense")
    else:
        ui.label("No access log entries yet.").classes("text-sm text-grey-6 mt-4")


_TAB_BUILDERS = {"Overview": _overview_tab, "Cost & Usage": _cost_tab,
                 "Reliability & Incidents": _reliability_tab, "Supabase": _supabase_tab,
                 "Access Log": _access_log_tab}


def _prewarm_supabase() -> None:
    """Run the Supabase/Overview network reads (rollup store incl. the previous
    window, Management API) in a worker thread so the synchronous render that
    follows hits warm caches and doesn't block the event loop."""
    import supabase_usage
    start, end = _supabase_window()[:2]
    days_back = max(math.ceil((dt.datetime.now(dt.timezone.utc) - start).total_seconds() / 86400), 1)
    ledger.fetch_meter_rollup(since_iso=start.isoformat(), until_iso=end.isoformat())
    ledger.fetch_meter_rollup(since_iso=(start - (end - start)).isoformat(), until_iso=start.isoformat())
    if STATE.get("active_tab") == "Supabase":
        supabase_usage.fetch_reported_usage(interval=supabase_usage.best_interval_for_days(days_back))


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
    active = STATE.get("active_tab")
    for name, fn in _TAB_BUILDERS.items():
        if name == active:
            STATE["dirty"].discard(name)
            fn.refresh()
        else:
            STATE["dirty"].add(name)
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
        await asyncio.to_thread(_check_alerts_light)
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
        result = await asyncio.to_thread(governance.check_pending_rules)
        _refresh_safely(governance_view)  # the tab may have been built before the cache filled
        await asyncio.sleep(_COMPLIANCE_RETRY_SEC if result.get("transient")
                            else _COMPLIANCE_INTERVAL_SEC)


@ui.page("/")
async def main_page() -> None:
    # Record access log (fire-and-forget, non-blocking)
    try:
        import access_log
        from nicegui import context as _ctx
        _req = _ctx.client.request
        asyncio.get_event_loop().run_in_executor(None, access_log.record_access, _req)
    except Exception:  # noqa: BLE001
        pass

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
            STATE["loading"] = True
            try:
                await asyncio.to_thread(fetch_stats, STATE["days"])
                if STATE.get("active_tab") in ("Supabase", "Overview"):
                    await asyncio.to_thread(_prewarm_supabase)
                refresh_all()
            finally:
                STATE["loading"] = False

        async def _apply_custom() -> None:
            start, end = start_date.value, end_date.value
            if not (start and end):
                ui.notify("Pick both a start and end date", type="warning")
                return
            if start > end:
                ui.notify("Start date must be on or before the end date", type="negative")
                return
            STATE["custom"] = (start, end)
            STATE["loading"] = True
            try:
                await asyncio.to_thread(fetch_stats)
                if STATE.get("active_tab") in ("Supabase", "Overview"):
                    await asyncio.to_thread(_prewarm_supabase)
                refresh_all()
            finally:
                STATE["loading"] = False

        async def _clear_custom() -> None:
            start_date.set_value(None)
            end_date.set_value(None)
            STATE["custom"] = None
            STATE["days"] = STATE.get("preset_days", 7)
            STATE["loading"] = True
            try:
                await asyncio.to_thread(fetch_stats, STATE["days"])
                if STATE.get("active_tab") in ("Supabase", "Overview"):
                    await asyncio.to_thread(_prewarm_supabase)
                refresh_all()
            finally:
                STATE["loading"] = False

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
                with ui.element("span").classes("shrink-0").bind_visibility_from(STATE, "loading")                         .mark("range-spinner"):
                    ui.spinner(size="sm", color="primary")
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
        ui.linear_progress(show_value=False).props("indeterminate size=3px")             .classes("w-full absolute bottom-0 left-0")             .bind_visibility_from(STATE, "loading").mark("range-progress")

    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-2 p-4 pt-2"):
        dashboard_body()  # stays centered, scrolls under the full-width sticky bar

    # FIXED 2026-08-30: was a direct (blocking) fetch_stats() call -- since
    # asyncio.to_thread offloads the blocking work to a thread rather than
    # the event loop, awaiting it here does NOT block other connected
    # clients (confirmed against NiceGUI's own docs/behavior, not assumed --
    # a fire-and-forget version was tried and reverted, see below). It's
    # correct for this one client's own page to wait for its own first
    # fetch, same as any normal "Loading…" page would.
    #
    # NOT fire-and-forget, on purpose: tried making main_page() return
    # immediately and let this run as a background task instead, so the
    # tabs/"Loading…" shell would appear instantly. That broke the
    # push-gated render tests -- they assert core content (e.g. "Total
    # cost") is visible within ~0.3s of open() returning, which two real
    # paginated Supabase round-trips can't reliably beat. It also didn't
    # even fix the actual problem (see below), so reverted rather than
    # widening what "ready" means across the whole test suite for a change
    # that wasn't the fix.
    #
    # TTL-GATED 2026-09-13: was an unconditional fetch on every single
    # connection -- found live (labeled-call-site diagnostic) that this was
    # firing every ~20-35s with zero manual navigation, matching bot/crawler
    # traffic against this fully-public page (no Cloudflare Access). STATE
    # is already one shared, module-level cache, not per-client, so skipping
    # a redundant refetch here is safe by construction: every connecting
    # client already reads the same STATE regardless, and any actual filter/
    # range change goes through its own dedicated handler (_set_range et al,
    # see _PAGE_LOAD_FETCH_TTL_SEC's docstring), which always fetches fresh
    # and is untouched by this gate. Estimated ~590MB/day Supabase egress
    # from this one pattern before the fix.
    async with _page_load_fetch_lock:
        last_fetch = STATE.get("last_fetch")
        page_load_stale = last_fetch is None or (
            not _is_bot_request() and
            (dt.datetime.now(dt.timezone.utc) - last_fetch).total_seconds() > _PAGE_LOAD_FETCH_TTL_SEC)
        if page_load_stale:
            await asyncio.to_thread(fetch_stats)
        if STATE.get("active_tab") in ("Supabase", "Overview"):
            await asyncio.to_thread(_prewarm_supabase)
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
