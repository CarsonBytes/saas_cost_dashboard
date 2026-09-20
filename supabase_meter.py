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

import datetime as dt
import json
import os
import time

APP = os.environ.get("SUPABASE_METER_APP", "unknown")
FILE = os.environ.get("SUPABASE_METER_FILE", "")
try:
    FLUSH_SEC = float(os.environ.get("SUPABASE_METER_SEC", "60"))
except ValueError:
    FLUSH_SEC = 60.0

# Cross-project rollup (2026-09-13): after each file flush, also POST the same
# per-minute batch to the `meter_rollup` Supabase table (~1 small write/min/
# app -- negligible against the tens of thousands of reads it attributes), so
# the dashboard can show per-app x per-endpoint attribution from ONE table
# instead of reaching into every project's local files. Reuses the standard
# SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY every app already has. Everything
# here is read lazily at flush time (dotenv commonly loads after this module
# is imported). SUPABASE_METER_ROLLUP=0 disables; otherwise a 404 (migration
# 004 not run yet) disables for the rest of the process lifetime -- rollout
# is not an ordered dependency.
_rollup_disabled = False

# Rollup-table `detail` column availability (dashboard migration 005):
# None = unprobed (include detail optimistically), True = confirmed,
# False = server rejected it (pre-005) -- remembered per process, detail
# then stays local-file-only. See _flush_rollup().
_ROLLUP_DETAIL_OK: bool | None = None


def _unknown_detail() -> dict:
    """Self-identifying fingerprint for UNLABELED traffic only (spec
    2026-09-20): when APP == "unknown", flush lines/rollup rows carry
    {host, pid, argv0} so the dashboard's Unlabeled-traffic card can point
    at the actual process instead of a dead end. Labeled traffic returns {}
    (zero extra bytes, zero new cardinality). Never raises."""
    if APP != "unknown":
        return {}
    try:
        import socket
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = "?"
    try:
        import os as _os
        import sys as _sys
        return {"host": host, "pid": _os.getpid(),
                "argv0": (_sys.argv[0] if _sys.argv else "?")[-80:]}
    except Exception:  # noqa: BLE001
        return {"host": host}


def _rollup_table() -> str:
    return os.environ.get("SUPABASE_METER_ROLLUP_TABLE", "meter_rollup")


def _rollup_force_off() -> bool:
    return os.environ.get("SUPABASE_METER_ROLLUP", "") == "0"

_counts: dict[str, int] = {}
_bytes: dict[str, int] = {}
_last_flush = 0.0
# The local file still flushes every FLUSH_SEC; the shared-table POST is
# throttled to one per ROLLUP_POST_SEC (default 5 min) -- each POST is a
# Supabase request, and 6 apps x 1/min was ~1,800+ requests/day for a table
# only ever read at hour granularity. Unposted batches wait in the pending
# buffer (same one that survives transient failures); the local file keeps
# every minute regardless.
ROLLUP_POST_SEC = float(os.environ.get("SUPABASE_METER_ROLLUP_SEC", "300"))
_rollup_last_post = 0.0


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
    _rollup_pending_counts.clear()
    _rollup_pending_bytes.clear()
    global _rollup_last_post
    _rollup_last_post = 0.0


def response_bytes(response) -> int:
    """Response payload size in REAL WIRE BYTES (what Supabase actually
    bills as egress) -- prefers response.num_bytes_downloaded, falls back to
    the Content-Length header, then len(content). Duck-typed, never raises
    -- 0 when unknown.

    FIXED 2026-09-15: len(content)/Content-Length-as-fallback measures the
    DEcompressed payload, not the compressed bytes actually transmitted.
    PostgREST/Supabase responses are gzip/brotli-compressed JSON sent with
    chunked transfer encoding (no Content-Length header at all, since
    compressed size isn't known upfront) -- httpx auto-decompresses
    transparently, so len(response.content) at that point is the decoded
    size. Confirmed live: a small test response measured 291 bytes via
    len(content) vs 225 via num_bytes_downloaded (matching the real
    Content-Length header) -- a ~30% gap on a trivial payload, and the real
    cause of this meter showing ~700MB/day of "egress" against Supabase's
    own billed ~75MB/day (an ~10x gap on the far more repetitive, more
    compressible JSON these endpoints actually return).
    num_bytes_downloaded counts bytes read off the actual transport/socket,
    so it reflects wire bytes regardless of compression -- this is the
    correct metric to use everywhere in this file.

    FIXED 2026-09-14 (kept below num_bytes_downloaded, still needed): study's
    usage is via response_hook() attached to httpx's "response" event hook
    (core/db.py's _instrument()) -- httpx fires that hook BEFORE the body is
    read for a non-streamed request (Client.send() only calls response.read()
    itself afterward), so num_bytes_downloaded/response.content would both
    read as empty/raise ResponseNotRead without this. response.read() here
    performs the real network read (populating num_bytes_downloaded
    correctly too), and is a safe no-op if the body was already read (the
    case for every manual httpx.get()-then-record() call site in this repo,
    which all call this on an already-completed response outside of any
    event hook)."""
    try:
        response.read()
    except Exception:  # noqa: BLE001
        pass
    try:
        n = response.num_bytes_downloaded
        if n:
            return max(0, int(n))
    except Exception:  # noqa: BLE001
        pass
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
    now = time.time()
    if now - _last_flush < FLUSH_SEC:
        return
    _last_flush = now
    if not _counts:
        return
    counts, byts = dict(_counts), dict(_bytes)
    line = {"ts": now, "app": APP, "counts": counts, "bytes": byts}
    detail = _unknown_detail()
    if detail:
        line["detail"] = detail
    line = json.dumps(line)
    took = False
    if FILE:
        took = True
        try:
            with open(FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 -- metering must never break the caller
            pass
    took = _flush_rollup(now, counts, byts) or took
    if took:
        # A sink took the batch -- reset the live view. With no sink at all
        # (memory-only mode, e.g. unit tests) counts stay put so snapshot()
        # keeps showing them instead of vanishing on the first flush tick.
        _counts.clear()
        _bytes.clear()


_rollup_pending_counts: dict[str, int] = {}
_rollup_pending_bytes: dict[str, int] = {}


def _flush_rollup(now: float, counts: dict[str, int], byts: dict[str, int]) -> bool:
    """POST one minute's per-endpoint rows to the rollup table, merged with
    any previously-failed batch so a transient error doesn't silently drop
    data -- only a confirmed 2xx response clears the pending buffer.

    FIXED 2026-09-17: the old version returned nothing on success (falsy),
    and _maybe_flush() cleared _counts/_bytes anyway once the FILE sink
    succeeded -- so a rollup POST that failed for any reason OTHER than 404
    (timeout, 5xx, transient network error) silently lost that window's
    data forever, with no retry. Found live: study-app's local JSONL file
    had 61 requests in a 10-minute window while meter_rollup had only 33 for
    the same app/window -- a real, variable undercount that made every
    cross-project total this app's reconciliation tab (and every
    "ecosystem-wide" estimate derived from it) systematically low by an
    unpredictable amount. The pending buffer is separate from _counts/
    _bytes (which stay exactly as before, feeding the FILE sink and
    snapshot()) so a rollup outage never affects local per-endpoint
    reporting, and a FILE-write failure never affects rollup delivery.

    stdlib urllib (NOT httpx) so this module stays dependency-free -- and so
    the write itself never passes through a metered code path (no self-
    counting). Never raises.

    `detail` (host/pid/argv0 for unknown-app traffic) rides along only when
    the server has the column (dashboard migration 005): inclusion is
    probe-and-remember per process (_ROLLUP_DETAIL_OK). A rejection naming
    `detail` disables it for the rest of the process lifetime (pending rows
    are kept and retried without it next tick); every other failure keeps
    the old retry semantics."""
    global _rollup_disabled, _rollup_last_post, _ROLLUP_DETAIL_OK
    if _rollup_disabled or _rollup_force_off():
        return False
    for ep, n in counts.items():
        _rollup_pending_counts[ep] = _rollup_pending_counts.get(ep, 0) + n
    for ep, n in byts.items():
        _rollup_pending_bytes[ep] = _rollup_pending_bytes.get(ep, 0) + n
    if not _rollup_pending_counts:
        return False
    if now - _rollup_last_post < ROLLUP_POST_SEC:
        return False
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return False
    import urllib.request
    detail = _unknown_detail() if _ROLLUP_DETAIL_OK is not False else {}
    rows = [{"ts": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
             "app": APP, "endpoint": ep,
             "requests": n, "bytes": _rollup_pending_bytes.get(ep, 0),
             **({"detail": detail} if detail else {})}
            for ep, n in _rollup_pending_counts.items()]
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/rest/v1/{_rollup_table()}",
            data=json.dumps(rows).encode("utf-8"),
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 404:
                _rollup_disabled = True  # migration 004 not run yet; stop trying
                _rollup_pending_counts.clear()
                _rollup_pending_bytes.clear()
                return False
            if 200 <= resp.status < 300:
                _rollup_last_post = now
                _rollup_pending_counts.clear()
                _rollup_pending_bytes.clear()
                if detail:
                    _ROLLUP_DETAIL_OK = True
                return True
            return False  # non-2xx, non-404: leave pending, retry next tick
    except Exception as e:  # noqa: BLE001 -- never break the caller; a 404
        # (raised as HTTPError, not returned) means the same thing. Any
        # OTHER exception (timeout, connection reset, ...) leaves the
        # pending buffer intact for the next flush to retry -- see this
        # function's docstring for why that matters.
        if "404" in str(e):
            _rollup_disabled = True
            _rollup_pending_counts.clear()
            _rollup_pending_bytes.clear()
        elif detail:
            try:
                body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
            except Exception:  # noqa: BLE001
                body = ""
            if "detail" in body or "detail" in str(e):
                # Pre-005 server: unknown `detail` key rejected. Remember,
                # keep pending (retried without detail next tick).
                _ROLLUP_DETAIL_OK = False
        return False


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
