"""Direct read of the shared Supabase `llm_calls` ledger -- REPLACES the
usage-stats Edge Function (D:\\adaptive_study_platform\\supabase\\functions\\
usage-stats), which turned out to 404 (never deployed, discovered 2026-07-28).
At this data volume (low hundreds of rows/day) fetching the window and
aggregating here is simpler than depending on a second deployable, and this
app already needs an httpx client either way.

Project/provider attribution: study populates the real `project`/`call_type`/
`provider` columns on every write; quant and event_radar do not (they only
write `purpose`, `model`, tokens, cost, latency) -- so those columns come back
null for their rows and we fall back to parsing the `purpose` prefix
("quant:kind" / "events:kind"), same convention as every other reader of this
table (event_radar's app/llm_logging.py::_project_of(), quant's
analyst/usage_log.py::_project_of()).
"""
from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

_SELECT = "project,call_type,provider,environment,model,purpose,prompt_tokens,completion_tokens,cost_usd,latency_ms,created_at"

# HKT has no DST, always UTC+8, so no zoneinfo/tzdata dependency needed. Mirrors
# quant's analyst/usage_log.py::_hkt_today_start_utc() and event_radar's
# app/llm_logging.py::hkt_today_start_utc() -- ported rather than reinvented,
# since the whole point is every reader of this shared ledger agreeing on the
# same "today" (this project previously used UTC everywhere -- a real bug: a
# call made at 3am HKT falls on the *previous* UTC calendar date).
_HKT = dt.timezone(dt.timedelta(hours=8))


def _hkt_today_start_utc() -> dt.datetime:
    hkt_midnight = dt.datetime.now(_HKT).replace(hour=0, minute=0, second=0, microsecond=0)
    return hkt_midnight.astimezone(dt.timezone.utc)


def _hkt_date_str(created_at: str) -> str:
    """The HKT calendar date (YYYY-MM-DD) a `created_at` UTC timestamp falls on."""
    instant = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(_HKT).strftime("%Y-%m-%d")


def to_hkt(instant: str | dt.datetime) -> dt.datetime:
    """Convert a UTC timestamp -- ISO-8601 string (as stored in the ledger /
    alert/incident state files) or a naive-UTC datetime -- to HKT. Reused for
    every user-facing timestamp display (FIXED 2026-08-15: the incident log,
    alert history, and last-refreshed stamp had regressed to raw UTC)."""
    if isinstance(instant, str):
        instant = dt.datetime.fromisoformat(instant.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=dt.timezone.utc)
    elif instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(_HKT)

_UNTAGGED = "(untagged)"


def _project_of(purpose: str) -> str:
    if purpose.startswith("quant:"):
        return "quant"
    if purpose.startswith("events:"):
        return "events"
    return "study"


def _call_type_of(purpose: str) -> str:
    return purpose.split(":", 1)[1] if ":" in purpose else purpose


def _fill_fallback(row: dict) -> dict:
    purpose = row.get("purpose") or ""
    if not row.get("project") and purpose:
        row["project"] = _project_of(purpose)
    if not row.get("call_type") and purpose:
        row["call_type"] = _call_type_of(purpose)
    return row


def fetch_rows(days: int) -> list[dict]:
    """Raw rows for the trailing `days` days (HKT day boundary), most recent first.

    PAGINATES (FIXED 2026-08-15): PostgREST caps any single response at 1,000
    rows regardless of the requested limit, so an unpaginated fetch silently
    dropped everything older than the most recent ~1,000 rows -- a 90-day view
    was really showing ~2 weeks. Now loops on an offset parameter, 1,000 rows
    at a time, until a batch comes back shorter than the page size."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set -- see .env.example")
    since = _hkt_today_start_utc() - dt.timedelta(days=days - 1)
    page_size = 1000
    rows: list[dict] = []
    offset = 0
    while True:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            params={
                "select": _SELECT,
                "created_at": f"gte.{since.isoformat()}",
                "order": "created_at.desc",
                "limit": str(page_size),
                "offset": str(offset),
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
    return [_fill_fallback(r) for r in rows]


# Missing-value label per field: most fields fall back to the generic
# "(untagged)", but an unset `environment` (study/events/regtech never set it,
# and older quant rows predate it) reads more honestly as "(no environment)"
# -- "(untagged)" made users wonder what was meant (FIXED 2026-08-15).
_FIELD_FALLBACKS = {"environment": "(no environment)"}


def _bucket_key(row: dict, field: str) -> str:
    v = row.get(field)
    if v is None or v == "":
        return _FIELD_FALLBACKS.get(field, _UNTAGGED)
    return str(v)


def aggregate_by(rows: list[dict], fields: list[str]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(_bucket_key(row, f) for f in fields)
        b = buckets.setdefault(key, {f: k for f, k in zip(fields, key)} |
            {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0})
        b["calls"] += 1
        b["cost_usd"] += row.get("cost_usd") or 0
        b["prompt_tokens"] += row.get("prompt_tokens") or 0
        b["completion_tokens"] += row.get("completion_tokens") or 0
        b["latency_ms"] += row.get("latency_ms") or 0
    out = list(buckets.values())
    for b in out:
        b["cost_usd"] = round(b["cost_usd"], 6)
    out.sort(key=lambda b: b["calls"], reverse=True)
    return out


def daily_by_project(rows: list[dict]) -> dict:
    """Per-project daily (HKT calendar day) cost, for a stacked trend chart --
    a single total line can't tell you which project caused a given day's
    spike."""
    dates = sorted({_hkt_date_str(r["created_at"]) for r in rows})
    projects = sorted({_bucket_key(r, "project") for r in rows})
    matrix = {p: {d: 0.0 for d in dates} for p in projects}
    for r in rows:
        matrix[_bucket_key(r, "project")][_hkt_date_str(r["created_at"])] += r.get("cost_usd") or 0
    return {
        "dates": dates,
        "series": [{"project": p, "data": [round(matrix[p][d], 6) for d in dates]} for p in projects],
    }


def build_stats(rows: list[dict], days: int) -> dict:
    daily: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    for row in rows:
        day = _hkt_date_str(row["created_at"])
        d = daily[day]
        d["calls"] += 1
        d["cost_usd"] += row.get("cost_usd") or 0
    daily_series = [{"date": d, "calls": v["calls"], "cost_usd": round(v["cost_usd"], 6)}
                     for d, v in sorted(daily.items())]

    return {
        "range_days": days,
        "total_calls": len(rows),
        "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 6),
        "total_prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
        "total_completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
        "by_project": aggregate_by(rows, ["project"]),
        "by_call_type": aggregate_by(rows, ["project", "call_type"]),
        "by_model": aggregate_by(rows, ["model"]),
        "by_environment": aggregate_by(rows, ["project", "environment"]),
        "by_provider": aggregate_by(rows, ["provider"]),
        "by_project_call_type_model": aggregate_by(rows, ["project", "call_type", "model"]),
        "daily_series": daily_series,
        "daily_by_project": daily_by_project(rows),
    }


def fetch_stats(days: int) -> dict:
    return build_stats(fetch_rows(days), days)


def today_cost(rows: list[dict]) -> float:
    today = dt.datetime.now(_HKT).strftime("%Y-%m-%d")
    return round(sum(r.get("cost_usd") or 0 for r in rows if _hkt_date_str(r["created_at"]) == today), 6)


def per_1k_usd(bucket: dict) -> float:
    tok = bucket["prompt_tokens"] + bucket["completion_tokens"]
    return (bucket["cost_usd"] / tok * 1000) if tok else float("inf")


def efficiency_ranking(buckets: list[dict]) -> list[dict]:
    """Buckets (e.g. stats["by_model"]) ranked cheapest-first by $/1K tokens.
    Excludes zero-cost/zero-token buckets -- there's nothing to rank there."""
    ranked = [b | {"per_1k_usd": per_1k_usd(b)} for b in buckets
              if b["cost_usd"] > 0 and (b["prompt_tokens"] + b["completion_tokens"]) > 0]
    ranked.sort(key=lambda b: b["per_1k_usd"])
    return ranked


def latency_ranking(buckets: list[dict]) -> list[dict]:
    """Buckets (e.g. stats["by_call_type"]) ranked slowest-first by average
    latency -- the raw per-call latency_ms was already being fetched but
    never surfaced anywhere."""
    ranked = [b | {"avg_latency_ms": round(b["latency_ms"] / b["calls"])} for b in buckets
              if b["calls"] > 0]
    ranked.sort(key=lambda b: b["avg_latency_ms"], reverse=True)
    return ranked


def with_cost_per_call(buckets: list[dict]) -> list[dict]:
    return [b | {"cost_per_call": round(b["cost_usd"] / b["calls"], 6) if b["calls"] else 0}
            for b in buckets]


# The specific migration question this table exists to answer: which
# project/call_type combinations are still calling the older model instead
# of the current one. Not a general "flag anything that isn't CURRENT_MODEL"
# rule -- embeddings/deepseek calls are legitimately different models for a
# different purpose, not stragglers.
LEGACY_MODEL = "gpt-4o-mini"
CURRENT_MODEL = "gpt-5-mini"


def legacy_model_usage(stats: dict) -> list[dict]:
    return [b for b in stats["by_project_call_type_model"] if b["model"] == LEGACY_MODEL]


def attribution_quality(stats: dict) -> dict:
    """What share of calls/cost actually carry a real provider tag, vs falling
    into the "(untagged)" bucket -- quant and event_radar's rows do today
    (see module docstring), so this is a direct, visible measure of that gap,
    and of any future fix to it."""
    total_calls = stats["total_calls"]
    total_cost = stats["total_cost_usd"]
    untagged = next((b for b in stats["by_provider"] if b["provider"] == _UNTAGGED), None)
    untagged_calls = untagged["calls"] if untagged else 0
    untagged_cost = untagged["cost_usd"] if untagged else 0.0
    return {
        "calls_tagged_pct": round((1 - untagged_calls / total_calls) * 100, 1) if total_calls else 100.0,
        "cost_tagged_pct": round((1 - untagged_cost / total_cost) * 100, 1) if total_cost else 100.0,
    }


def top_spender_insight(stats: dict) -> str | None:
    """One-sentence auto-generated highlight: which project drove the biggest
    share of spend in the current window. None if there's no cost yet."""
    total = stats["total_cost_usd"]
    if total <= 0 or not stats["by_project"]:
        return None
    top = max(stats["by_project"], key=lambda b: b["cost_usd"])
    if top["cost_usd"] <= 0:
        return None
    share = top["cost_usd"] / total * 100
    cheapest_providers = efficiency_ranking(stats["by_provider"])
    msg = f"{top['project']} drove {share:.0f}% of spend (${top['cost_usd']:.4f}) over the last {stats['range_days']}d"
    if cheapest_providers:
        cheapest = cheapest_providers[0]
        msg += f"; cheapest provider was {cheapest['provider']} (${cheapest['per_1k_usd']:.6f}/1K tok)"
    return msg
