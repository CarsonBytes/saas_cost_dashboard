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

# ADDED 2026-09-15: the real billed-egress number lives only on Supabase's
# own dashboard (Settings -> Usage -> Egress) -- confirmed no Management API
# endpoint exposes it (checked usage.api-counts, billing/addons, invoice/
# subscription/cost/meter/consumption paths in the full OpenAPI spec: none
# return a bytes/egress figure). Self-metered bytes are a real-time estimate,
# not ground truth, and were themselves found wrong by ~10x once (see
# response_bytes()'s num_bytes_downloaded fix) -- this lets the operator
# paste in what the Supabase UI actually shows once in a while, so future
# drift between the estimate and reality is caught by inspection instead of
# silently trusted for weeks.
_ANCHOR_FILE = Path(__file__).parent / "state" / "egress_anchor.json"


def load_egress_anchor() -> dict | None:
    """{"mb": float, "date": "YYYY-MM-DD", "note": str} last saved by the
    operator, or None if never set."""
    try:
        return json.loads(_ANCHOR_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def save_egress_anchor(mb: float, date: str, note: str = "") -> None:
    _ANCHOR_FILE.write_text(
        json.dumps({"mb": mb, "date": date, "note": note}), encoding="utf-8")


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


def fetch_reported_usage(since_iso: str | None = None,
                         until_iso: str | None = None) -> dict | None:
    """Hourly-cached Management API payload (or None when unconfigured /
    unreachable -- never raises). Shape: {"fetched_at": epoch, "payload": ...}.
    When since_iso/until_iso are provided, uses iso_timestamp_start/end params
    instead of the interval param (Management API supports both)."""
    now = time.time()
    cache_key = f"{since_iso or ''}|{until_iso or ''}"
    try:
        cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if (now - cached.get("fetched_at", 0) < _CACHE_SEC
                and cached.get("payload")
                and cached.get("cache_key") == cache_key):
            return cached
    except (FileNotFoundError, ValueError):
        pass
    if not configured():
        return None
    try:
        params: dict = {}
        if since_iso or until_iso:
            if since_iso:
                params["iso_timestamp_start"] = since_iso
            if until_iso:
                params["iso_timestamp_end"] = until_iso
        else:
            params["interval"] = "1day"
        resp = httpx.get(
            f"https://api.supabase.com/v1/projects/{_project_ref()}/analytics/endpoints/usage.api-counts",
            params=params,
            headers={"Authorization": f"Bearer {_mgmt_token()}"},
            timeout=15)
        resp.raise_for_status()
        result = {"fetched_at": now, "payload": resp.json(),
                  "cache_key": cache_key}
    except Exception:  # noqa: BLE001 -- reconciliation is informational, never fatal
        return None
    try:
        _CACHE_FILE.write_text(json.dumps(result), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def reported_requests(cached: dict | None) -> tuple[int | None, str]:
    """(REST request total for the selected window, note). The real (verified
    live 2026-09-13) usage.api-counts shape is
    {"result": [{"timestamp", "total_rest_requests", "total_auth_requests",
    "total_realtime_requests", "total_storage_requests"}, ...]} -- hourly
    buckets. Only total_rest_requests is summed: the self-meters this gets
    compared against count REST calls only (supabase-py/httpx to /rest/v1/*),
    so auth/realtime/storage would inflate the reported side without a matching
    metered side. Falls back to defensive generic parsing if the shape ever
    changes, so a future API change reads as 'unrecognized shape' rather than
    a silently wrong number."""
    if not cached:
        return None, "management API not configured (SUPABASE_MANAGEMENT_TOKEN)"
    payload = cached.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        buckets = payload["result"]
        if buckets and all(isinstance(b, dict) and "total_rest_requests" in b for b in buckets):
            total = sum(b.get("total_rest_requests") or 0 for b in buckets)
            return int(total), f"summed total_rest_requests across {len(buckets)} hourly buckets"
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
