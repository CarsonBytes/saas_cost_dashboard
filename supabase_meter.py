"""In-process Supabase REST call meter -- answers "which app/table is driving
our Supabase egress?" without depending on Supabase's own edge_logs (free-tier
retention is ~1h, useless for attributing a per-day total after the fact).

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
_last_flush = 0.0


def configure(app: str | None = None, path: str | None = None) -> None:
    """Override the env-derived app label / flush file at import time (e.g.
    ledger.py pins the dashboard's state/ path so it works with no env)."""
    global APP, FILE
    if app is not None:
        APP = app
    if path is not None:
        FILE = path


def record(method: str, table: str) -> None:
    """Count one real Supabase REST call. Call AFTER the HTTP round-trip only
    -- cached reads that never hit the network must not be recorded, or the
    meter measures code paths instead of egress."""
    _counts[f"{method} {table}"] = _counts.get(f"{method} {table}", 0) + 1
    _maybe_flush()


def snapshot() -> dict[str, int]:
    """Current in-memory counts (this process, since the last flush)."""
    return dict(_counts)


def reset() -> None:
    """Clear in-memory counts (tests; operators reading the file don't need it)."""
    _counts.clear()


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
    line = json.dumps({"ts": now, "app": APP, "counts": dict(_counts)})
    _counts.clear()
    try:
        with open(FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 -- metering must never break the caller
        pass


def read_totals(path: str, since_ts: float = 0.0) -> dict[str, int]:
    """Aggregate flushed JSONL lines at `path` into per-key totals (operators /
    dashboard readout). Malformed lines are skipped, never raised."""
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
                for key, n in (entry.get("counts") or {}).items():
                    totals[key] = totals.get(key, 0) + int(n)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return totals
