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
    """Raw rows for the trailing `days` days (UTC day boundary), most recent first."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set -- see .env.example")
    since = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
        - dt.timedelta(days=days - 1)
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/llm_calls",
        params={
            "select": _SELECT,
            "created_at": f"gte.{since.isoformat()}",
            "order": "created_at.desc",
            "limit": "20000",
        },
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return [_fill_fallback(r) for r in resp.json()]


def _bucket_key(row: dict, field: str) -> str:
    v = row.get(field)
    return "(untagged)" if v is None or v == "" else str(v)


def aggregate_by(rows: list[dict], fields: list[str]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(_bucket_key(row, f) for f in fields)
        b = buckets.setdefault(key, {f: k for f, k in zip(fields, key)} |
            {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0})
        b["calls"] += 1
        b["cost_usd"] += row.get("cost_usd") or 0
        b["prompt_tokens"] += row.get("prompt_tokens") or 0
        b["completion_tokens"] += row.get("completion_tokens") or 0
    out = list(buckets.values())
    for b in out:
        b["cost_usd"] = round(b["cost_usd"], 6)
    out.sort(key=lambda b: b["calls"], reverse=True)
    return out


def build_stats(rows: list[dict], days: int) -> dict:
    daily: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    for row in rows:
        day = row["created_at"][:10]
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
        "daily_series": daily_series,
    }


def fetch_stats(days: int) -> dict:
    return build_stats(fetch_rows(days), days)


def today_cost(rows: list[dict]) -> float:
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    return round(sum(r.get("cost_usd") or 0 for r in rows if r["created_at"][:10] == today), 6)


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
    providers = [b for b in stats["by_provider"] if b["cost_usd"] > 0]
    cheapest = None
    if providers:
        def per_1k(b: dict) -> float:
            tok = b["prompt_tokens"] + b["completion_tokens"]
            return (b["cost_usd"] / tok * 1000) if tok else float("inf")
        cheapest = min(providers, key=per_1k)
    msg = f"{top['project']} drove {share:.0f}% of spend (${top['cost_usd']:.4f}) over the last {stats['range_days']}d"
    if cheapest:
        msg += f"; cheapest provider was {cheapest['provider']} (${per_1k(cheapest):.6f}/1K tok)"
    return msg
