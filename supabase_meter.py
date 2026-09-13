"""In-process Supabase REST call meter -- answers "which app/table is driving
our Supabase egress?" without depending on Supabase's own edge_logs (free-tier
retention is ~1h, useless for attributing a per-day total after the fact).
Each entry counts requests AND response bytes per "METHOD table" key.

ADDED 2026-09-13: the 40-83K req/day egress investigation (usage.api-counts)
could only sample the last hour of edge_logs, so per-app attribution was
guesswork. This module is the fix for next time: every Supabase REST call a
process makes is counted here, in-process, keyed "METHOD table", and flushed
as one JSONL line per minute to a local file. Zero network, zero deps, never
raises -- file IO failures are swallowed so metering can never break the
caller. Copy this file verbatim into each Python repo in the ecosystem
(quant, event-radar, study); they share no package, so a copy is the
integration, not an import.

Usage -- one line at each real (non-cached) Supabase call site:
    supabase_meter.record("GET", "llm_calls")

Knobs (env):
    SUPABASE_METER_APP   app label written into each flushed line (default: "unknown")
    SUPABASE_METER_FILE  JSONL flush target (default: "" = memory only, no file IO)
    SUPABASE_METER_SEC   flush interval seconds (default: 60)
"""
from __future__ import annotations

import json
import os
import time

APP = os.environ.get("SUPABASE_METER_APP", "unknown")
FILE = os.environ.get("SUPABASE_METER_FILE", "")
try:
    FLUSH_SEC = float(os.environ.get("SUPABASE_METER_SEC", "60"))
except ValueError:
    FLUSH_SEC = 60.0

_counts: dict[str, int] = {}
_bytes: dict[str, int] = {}
_last_flush = 0.0


def configure(app: str | None = None, path: str | None = None) -> None:
    """Override the env-derived app label / flush file at import time (e.g.
    ledger.py pins the dashboard's state/ path so it works with no env)."""
    global APP, FILE
    if app is not None:
        APP = app
    if path is not None:
        FILE = path


def record(method: str, table: str, nbytes: int = 0) -> None:
    """Count one real Supabase REST call. Call AFTER the HTTP round-trip only
    -- cached reads that never hit the network must not be recorded, or the
    meter measures code paths instead of egress. `nbytes` is the response
    payload size (see response_bytes()); 0 when unknown -- the call still
    counts, it just contributes no size."""
    key = f"{method} {table}"
    _counts[key] = _counts.get(key, 0) + 1
    if nbytes:
        _bytes[key] = _bytes.get(key, 0) + nbytes
    _maybe_flush()


def snapshot() -> dict[str, int]:
    """Current in-memory counts (this process, since the last flush)."""
    return dict(_counts)


def snapshot_bytes() -> dict[str, int]:
    """Current in-memory response-byte totals, same keys as snapshot()."""
    return dict(_bytes)


def reset() -> None:
    """Clear in-memory counts (tests; operators reading the file don't need it)."""
    _counts.clear()
    _bytes.clear()


def response_bytes(response) -> int:
    """Response payload size without disturbing the caller: prefers the
    Content-Length header (no body touch), falls back to len(content).
    Duck-typed, never raises -- 0 when unknown."""
    try:
        cl = response.headers.get("content-length")
        if cl is not None:
            return max(0, int(cl))
    except Exception:  # noqa: BLE001
        pass
    try:
        return max(0, len(response.content or b""))
    except Exception:  # noqa: BLE001
        return 0


def _maybe_flush() -> None:
    global _last_flush
    if not FILE:
        return
    now = time.time()
    if now - _last_flush < FLUSH_SEC:
        return
    _last_flush = now
    if not _counts:
        return
    line = json.dumps({"ts": now, "app": APP,
                       "counts": dict(_counts), "bytes": dict(_bytes)})
    _counts.clear()
    _bytes.clear()
    try:
        with open(FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 -- metering must never break the caller
        pass


def response_hook(response) -> None:
    """httpx `response` event-hook: auto-meters any Supabase PostgREST call by
    parsing METHOD + table straight from the request URL -- no per-call-site
    editing, and a new call site can never silently go unmetered (the failure
    mode that let event-radar's original O(n) poll hide as long as it did).
    Attach once per session:
        session.event_hooks["response"].append(supabase_meter.response_hook)
    Non-PostgREST URLs are ignored. Duck-typed (no httpx import) so this
    module stays dependency-free. Never raises."""
    try:
        req = response.request
        _, _, rest = req.url.path.partition("/rest/v1/")
        if not rest:
            return
        table = rest.split("/", 1)[0]
        if not table:
            return
        record(req.method, table, response_bytes(response))
    except Exception:  # noqa: BLE001
        pass


def _read_lines(path: str, field: str, since_ts: float = 0.0,
                app: str | None = None) -> dict[str, int]:
    """Aggregate one field ("counts" | "bytes") of flushed JSONL lines.
    `app` filters to one writer (several services can share a file -- e.g.
    study + study-demo). Malformed lines are skipped, never raised."""
    totals: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("ts", 0) < since_ts:
                    continue
                if app is not None and entry.get("app") != app:
                    continue
                for key, n in (entry.get(field) or {}).items():
                    totals[key] = totals.get(key, 0) + int(n)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return totals


def read_totals(path: str, since_ts: float = 0.0,
                app: str | None = None) -> dict[str, int]:
    """Aggregate flushed JSONL lines at `path` into per-key request totals
    (operators / dashboard readout)."""
    return _read_lines(path, "counts", since_ts, app)


def read_bytes(path: str, since_ts: float = 0.0,
               app: str | None = None) -> dict[str, int]:
    """Same, for response-byte totals -- the size half of the picture."""
    return _read_lines(path, "bytes", since_ts, app)
