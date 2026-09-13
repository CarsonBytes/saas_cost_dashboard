"""Supabase-reported usage -- the other half of egress reconciliation.

The self-meter (supabase_meter.py) sees only what each app counts itself;
Supabase's own counters see the whole project (all six apps, all protocols,
wire bytes with headers/TLS). This module polls the Management API's
usage/api-counts for the project total and caches it hourly to state/ --
the tab then shows reported-vs-metered side by side instead of asking anyone
to trust one number.

Needs one new secret: SUPABASE_MANAGEMENT_TOKEN (a `sbp_...` account token
with usage:read -- NOT the service_role key, which the Management API won't
accept). Without it every function here returns None and the tab says so;
nothing raises. The project ref is derived from the SUPABASE_URL host
(https://<ref>.supabase.co), so no second env var to keep in sync.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_CACHE_FILE = Path(__file__).parent / "state" / "supabase_usage.json"
_CACHE_SEC = 3600


def _mgmt_token() -> str:
    # Read lazily (not at import): the operator adds the token to .env long
    # after first import in long-lived processes and tests.
    return os.environ.get("SUPABASE_MANAGEMENT_TOKEN", "")


def _project_ref() -> str:
    host = (os.environ.get("SUPABASE_URL", "").rstrip("/").split("://")[-1]
            .split("/")[0])
    return host.split(".")[0] if host.endswith(".supabase.co") else ""


def configured() -> bool:
    """Whether a management token + derivable project ref exist."""
    return bool(_mgmt_token() and _project_ref())


def fetch_reported_usage() -> dict | None:
    """Hourly-cached Management API payload (or None when unconfigured /
    unreachable -- never raises). Shape: {"fetched_at": epoch, "payload": ...}."""
    now = time.time()
    try:
        cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if now - cached.get("fetched_at", 0) < _CACHE_SEC and cached.get("payload"):
            return cached
    except (FileNotFoundError, ValueError):
        pass
    if not configured():
        return None
    try:
        resp = httpx.get(
            f"https://api.supabase.com/v1/projects/{_project_ref()}/usage/api-counts",
            headers={"Authorization": f"Bearer {_mgmt_token()}"},
            timeout=15)
        resp.raise_for_status()
        result = {"fetched_at": now, "payload": resp.json()}
    except Exception:  # noqa: BLE001 -- reconciliation is informational, never fatal
        return None
    try:
        _CACHE_FILE.write_text(json.dumps(result), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def reported_requests_24h(cached: dict | None) -> tuple[int | None, str]:
    """(trailing-24h-ish request total, note). The api-counts shape isn't
    contractual -- parse defensively across the shapes seen in the wild and
    say which one matched, so a silent API change reads as 'unknown shape'
    rather than a confident wrong number."""
    if not cached:
        return None, "management API not configured (SUPABASE_MANAGEMENT_TOKEN)"
    payload = cached.get("payload")
    entries: list | dict = []
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        entries = data
    elif isinstance(payload, list):
        entries = payload
    if isinstance(entries, dict):  # {"<action>": n, ...} form
        total = sum(v for v in entries.values() if isinstance(v, (int, float)))
        return int(total), "summed per-action counters"
    if isinstance(entries, list) and entries:
        total = 0
        for e in entries:
            if isinstance(e, dict):
                for k in ("count", "requests", "total", "value"):
                    if isinstance(e.get(k), (int, float)):
                        total += e[k]
                        break
        # trailing entries ≈ trailing 24h for the daily-bucketed form; say so.
        return int(total), f"summed {len(entries)} buckets (shape keys: {sorted(entries[0])})"
    return None, f"unrecognized api-counts shape: {str(payload)[:120]}"
