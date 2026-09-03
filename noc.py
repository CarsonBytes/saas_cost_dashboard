"""Personal NOC, Phase 1 -- health engine for the monitored agents listed in
services.SERVICES.

Two-layer health per monitored agent:
  liveness  -- plain reachability probe of every monitored link (the same
               semantics as the old services.refresh_statuses: *any* HTTP
               response counts as up, since these apps sit behind Cloudflare
               Access and answer with a redirect rather than a 200).
  readiness -- freshness of the agent's most recent write to the shared
               Supabase `llm_calls` ledger (writers only: quant/events/study),
               compared against a per-agent threshold. A reachable-but-stale
               agent is "degraded" -- the failure mode a plain uptime check
               misses entirely. Portfolio and AI Regulation Radar have no
               meaningful "last write" signal, so liveness alone suffices.

Plus, per agent: a "blocked by" badge when an unhealthy agent's failure is
explained by a confirmed-down shared dependency (Supabase / the shared LLM
API key); restart authority (auto_heal restarts the agent's own Docker
container, alert_only pushes a deduped Telegram alert, none does nothing); a
3-restarts-per-hour cooldown that locks the agent until explicitly unlocked
from the UI; a general incident log; and a 7-day uptime percentage from the
check history this loop already produces.

Restart triggers (auto_heal only): a liveness failure always justifies a
restart; a STALE readiness result only justifies one for agents with a real,
enforced write cadence (services.py restart_on_staleness=True -- Quant
Paper's scan loop, Event Radar's ingest schedule). Usage-driven agents like
Study Platform degrade visibly on staleness but never restart from it --
restarting a container cannot produce usage (FIXED 2026-08-15: Study was
auto-restarting every ~6min while merely idle, locking and re-locking).

restart_on_staleness vs enforced_cadence (ADDED 2026-08-17): these look like
the same flag and, for every agent before Quant Live, were the same value --
but they answer different questions. restart_on_staleness is restart
AUTHORITY ("may staleness trigger an auto-restart"). enforced_cadence is a
DISPLAY/uptime fact ("is staleness here a real fault, or just this agent
being unused"). Quant Live has a real enforced cadence -- its staleness is a
genuine problem worth showing amber and alerting on -- but must never
auto-restart, since this dashboard's restart logic doesn't understand IBKR's
reconciliation state well enough to safely bounce a live trading agent.
enforced_cadence=True, restart_on_staleness=False: the one agent where they
diverge.

Threading: everything here is blocking (HTTP probes, Supabase reads, Docker
Engine API calls, file writes). Call refresh_health() via asyncio.to_thread
from async code, never from a page-render path -- restarts fire only from the
background loop.

Readiness exceptions (both needed to avoid false alarms):
  1. 5-minute grace after a dashboard-initiated restart (still warming up).
  2. Any agent with services.py market_hours_only=True (Quant Paper, Quant
     Live): skipped outside the real NYSE session -- computed in US Eastern
     time with DST via zoneinfo("America/New_York"), plus real US market
     holidays via pandas_market_calendars (the same approach quant's own
     dashboard/app.py::_market_open() + core/market_calendar.py use -- that
     exact DST bug was found and fixed there first, so this mirrors it rather
     than reinventing a fixed-HK-range that drifts an hour wrong twice a
     year). Originally a hardcoded "is this literally Quant Paper" name
     check; generalized to the field 2026-08-17 once Quant Live also needed
     it, rather than growing a second hardcoded name.

State: file-backed JSON (noc_state.json), same pattern as alerts.py's
alert_state.json -- incidents/restart counts/locks survive a dashboard
restart, and the state file is excluded from rsync/docker so the container's
copy is the only writer.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import alerts
import governance  # compliance_health (OVERDUE rules by agent) for quarantine
import ledger  # same Supabase connection config this app already reads the ledger with
import services

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

QUANT_PAPER = "Quant Trading (Paper)"

_RESTART_GRACE_SEC = 300           # 5 min warmup after a dashboard-initiated restart
_RESTART_WINDOW_SEC = 3600         # rolling hour for the cooldown counter
_RESTART_LOCK_COUNT = 3            # restarts within the window that trigger a lock
_UPTIME_DAYS = 7
_INCIDENT_LIMIT = 50

# Uptime-strip buckets (ADDED 2026-08-26): the daily ok/fail buckets behind
# _uptime_7d can't draw a status-page strip -- a day is too coarse to show
# WHEN in the week an agent was down. The health cycle additionally folds each
# result into fixed 6-hour slots, kept for the trailing 28 slots (one week).
# Bounded state: exactly 28 entries per agent.
_SLOT_SEC = 6 * 3600
_SLOT_COUNT = 28

# Manual quarantine (pause) / resume passcode gate (FIXED 2026-08-17): neither
# action had any authentication at all, and this dashboard is public with no
# Cloudflare Access -- anyone loading the page could pause a live trading
# agent's container in two clicks. A single shared passcode, checked
# server-side only (never in the browser, which would just leak it via
# view-source) -- proportionate for a single-operator dashboard, not a
# multi-user auth system. Unset = both actions stay disabled (fails closed).
_ACTION_PASSCODE = os.environ.get("NOC_ACTION_PASSCODE", "")
_AUTH_FAIL_LIMIT = 5               # failed attempts against one agent...
_AUTH_FAIL_WINDOW_SEC = 600        # ...within this window locks it out

# ADDED 2026-08-18: lives under state/, a named Docker volume (see
# docker-compose.yml), not directly in /app -- previously this sat in the
# container's writable layer with nothing mounted, so every redeploy wiped
# incidents/restarts/locks/quarantine flags. That's already caused two real
# problems live: an outage's evidence disappearing mid-investigation, and a
# quarantine flag getting cleared by a redeploy while the container it
# tracked was still genuinely paused, leaving auto-heal about to "restart"
# an intentionally-paused container. mkdir here so local (non-Docker) runs
# and the very first container start both just work.
_STATE_DIR = Path(__file__).parent / "state"
_STATE_DIR.mkdir(parents=True, exist_ok=True)
_STATE_FILE = _STATE_DIR / "noc_state.json"
_STATE_LOCK = threading.Lock()

_STATUS_CACHE: dict[str, dict] = {}

_LLM_API_BASE_URL = os.environ.get(
    "LLM_API_BASE_URL", "https://api.chatanywhere.tech/v1"
).rstrip("/")

# The restart-proxy sidecar (same compose project) is the ONLY component that
# holds the Docker socket; it validates the container name against an
# allow-list. This dashboard no longer mounts the socket at all (FIXED
# 2026-08-15: a public-facing dashboard holding raw Docker-socket access was
# host-level root reachable from the open internet).
_RESTART_PROXY_URL = os.environ.get(
    "RESTART_PROXY_URL", "http://restart-proxy:8096"
).rstrip("/")


# ---- market-hours (Quant Paper readiness exception) -------------------------

_year_cache: dict[int, set | None] = {}


def _trading_days_for_year(year: int) -> set | None:
    """NYSE trading-day SET for a year, cached per year (a schedule() call is
    not free and this is checked every health cycle). Returns None when the
    calendar package fails -- the caller then fails OPEN (treats every weekday
    as a trading day), mirroring quant's market_calendar.py: a missed holiday
    just means one readiness check that will correct itself, whereas failing
    closed could suppress the check entirely."""
    if year not in _year_cache:
        try:
            import pandas_market_calendars as mcal
            nyse = mcal.get_calendar("NYSE")
            sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
            _year_cache[year] = set(sched.index.date)
        except Exception as e:  # noqa: BLE001
            log.warning("noc: NYSE schedule fetch failed for %d, treating every weekday "
                        "as a trading day: %s", year, e)
            _year_cache[year] = None
    return _year_cache[year]


def is_us_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5:                          # Sat/Sun -- no calendar lookup needed
        return False
    trading_days = _trading_days_for_year(d.year)
    if trading_days is None:                      # calendar fetch failed -- fail open
        return True
    return d in trading_days


def nyse_session_open(now: dt.datetime | None = None) -> bool:
    """True during the regular NYSE session: Mon-Fri 9:30-16:00 US Eastern
    (DST-aware via America/New_York) on real NYSE trading days. Pass an aware
    `now` for testing; a naive `now` is treated as ET."""
    if now is None:
        now = dt.datetime.now(_ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    if not is_us_trading_day(now.date()):
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <= \
        now.replace(hour=16, minute=0, second=0, microsecond=0)


# ---- probes ----------------------------------------------------------------

def _probe(url: str) -> bool:
    """Reachability probe: *any* HTTP response (status < 500) counts as up."""
    try:
        resp = httpx.head(url, timeout=5, follow_redirects=True)
        return resp.status_code < 500
    except Exception:                             # noqa: BLE001
        return False


def _liveness(svc: dict) -> bool:
    """FIXED 2026-08-29: probed each of a service's own links sequentially
    (Python's all() over a generator), so a service with 2 Access-gated
    links paid the full redirect-chain latency of BOTH, one after another --
    harmless with 4 services, but LinkedIn's addition (2 gated links, ~1.6s +
    ~1.0s) made it the single slowest entry in the outer pool.map across
    services, stretching refresh_health()'s per-cycle duration enough to
    starve the shared asyncio default thread pool that fetch_stats's own
    to_thread call also draws from -- confirmed live via timing
    instrumentation, not assumed: intermittent 500s ('not ready after 3.0
    seconds') on unrelated tests appeared only after this service was added.
    A service's own links now probe concurrently, same pattern already used
    across services in refresh_health()."""
    urls = [url for _, url in svc["links"]]
    if len(urls) <= 1:
        return all(_probe(url) for url in urls)
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        return all(pool.map(_probe, urls))


def _dependency_probe_results() -> dict[str, bool]:
    """Raw per-dependency probe result for THIS cycle (True = healthy), from
    this dashboard's own vantage point (not from the agent under test)."""
    return {
        "Supabase": _probe(f"{ledger.SUPABASE_URL.rstrip('/')}/rest/v1/"),
        "LLM API": _probe(_LLM_API_BASE_URL),
    }


# Consecutive-cycle probe history per dependency, signed: positive = N
# consecutive healthy cycles, negative = N consecutive failing cycles.
# A single point-in-time probe is not a reliable enough signal to gate a
# restart decision (FIXED 2026-08-15: chatanywhere flapping between healthy
# and unhealthy across single cycles let three staleness-triggered restarts
# slip through inside one rolling hour and locked Quant Paper -- each restart
# fired during a cycle where the probe happened to read "up", even though the
# dependency was down more often than not that whole stretch).
_DEPS_STREAK: dict[str, int] = {}


def _update_dep_streak(dep: str, healthy: bool) -> int:
    """Advance the consecutive-run counter for one probe result. Returns the
    new streak."""
    cur = _DEPS_STREAK.get(dep, 0)
    _DEPS_STREAK[dep] = cur + 1 if healthy else (cur - 1 if cur <= 0 else -1)
    return _DEPS_STREAK[dep]


def _dep_confirmed_down(streak: int) -> bool:
    """Blocked-by (and restart suppression) requires the dependency to be down
    for >=2 consecutive cycles -- a single flaky probe read no longer flips
    the badge on and off."""
    return streak <= -2


def _dep_confirmed_healthy(streak: int) -> bool:
    """A dependency is 'stable' only after >=2 consecutive healthy cycles."""
    return streak >= 2


def _latest_write(svc: dict) -> str | None:
    """Most recent created_at (ISO 8601, UTC) for an agent's freshness signal,
    or None if the source has no rows. Default: the newest llm_calls row for
    the agent's project tag (LLM activity -- right for enforced-cadence agents
    like Quant Paper's scans and Event Radar's ingest). An agent with a
    `freshness_table` override reads that table instead -- Study Platform
    watches `answer_log`, because practice-mode correct answers never touch
    the LLM ledger (FIXED 2026-08-15: the card read "idle" right after the
    user answered several questions).

    `environment_tag`, when set, additionally filters on the ledger's
    `environment` column (ADDED 2026-08-17): Quant Paper and Quant Live share
    the same project_tag "quant", split only by environment "paper"/"live" --
    without this, monitoring both at once would let each one's freshness leak
    into the other's (Paper reading "fresh" off Live's writes, or vice versa).

    Raises on Supabase failure -- callers treat that as stale + let the
    dependency probe decide whether it's a blocked-by situation."""
    table = svc.get("freshness_table", "llm_calls")
    params = {"select": "created_at", "order": "created_at.desc", "limit": "1"}
    tag = svc.get("project_tag")
    if table == "llm_calls" and tag:
        params["project"] = f"eq.{tag}"
        env = svc.get("environment_tag")
        if env:
            params["environment"] = f"eq.{env}"
    resp = httpx.get(
        f"{ledger.SUPABASE_URL}/rest/v1/{table}",
        params=params,
        headers={"apikey": ledger.SUPABASE_SERVICE_ROLE_KEY,
                 "Authorization": f"Bearer {ledger.SUPABASE_SERVICE_ROLE_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["created_at"] if rows else None


def _latest_write_safe(svc: dict) -> str | None:
    try:
        return _latest_write(svc)
    except Exception:                             # noqa: BLE001
        return None


# ---- readiness -------------------------------------------------------------

def _readiness(svc: dict, now: dt.datetime, last_restart: float | None,
               last_write_ts: str | None) -> tuple[str, str]:
    """Readiness state for a monitored agent. Returns (state, detail) where
    state is "ok" | "stale" | "skipped" | "n/a". Pure -- testable without
    network; refresh_health() fetches last_write_ts first."""
    if not (svc.get("project_tag") or svc.get("freshness_table")):
        return "n/a", ""
    if last_restart and (now - dt.datetime.fromtimestamp(last_restart, tz=dt.timezone.utc)
                         ).total_seconds() < _RESTART_GRACE_SEC:
        return "skipped", "warming up"
    if svc.get("market_hours_only") and not nyse_session_open(now):
        return "skipped", "market closed"
    if last_write_ts is None:
        return "stale", "no recent ledger write"
    last = dt.datetime.fromisoformat(last_write_ts.replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    age_h = (now.astimezone(dt.timezone.utc) - last).total_seconds() / 3600
    if age_h * 3600 > svc["freshness_sec"]:
        return "stale", f"last write {age_h:.1f}h ago"
    return "ok", ""


# ---- restart authority, cooldown, lock ------------------------------------

def _restart_container(name: str) -> bool:
    """Restart the agent's own container through the restart-proxy sidecar --
    the only component that holds the Docker socket (it validates the name
    against its allow-list before touching the Engine API). The dashboard
    itself has no socket access, so a compromised dashboard can only trigger a
    restart of an allow-listed container, nothing else."""
    try:
        resp = httpx.post(f"{_RESTART_PROXY_URL}/restart",
                          json={"container": name}, timeout=60)
        return resp.status_code < 300 and bool(resp.json().get("ok"))
    except Exception as e:                        # noqa: BLE001
        log.warning("noc: restart of %s via proxy failed: %s", name, e)
        return False


def _proxy_action(action: str, name: str) -> bool:
    """Generic restart-proxy call (restart/pause/unpause). Same allow-list."""
    try:
        resp = httpx.post(f"{_RESTART_PROXY_URL}/{action}",
                          json={"container": name}, timeout=60)
        return resp.status_code < 300 and bool(resp.json().get("ok"))
    except Exception as e:                        # noqa: BLE001
        log.warning("noc: proxy %s of %s failed: %s", action, name, e)
        return False


def _container_stats_map() -> dict[str, dict]:
    """ADDED 2026-09-04 (memory-control spec item #4): one GET /stats call to
    the restart-proxy, covering every allow-listed container, so a cycle
    with N monitored agents costs one extra local Unix-socket round-trip, not
    N. Same trust boundary as restart/pause: the dashboard never touches the
    Docker socket itself. Best-effort -- an empty map here just means no
    agent gets a memory reading this cycle, never a crashed cycle."""
    try:
        resp = httpx.get(f"{_RESTART_PROXY_URL}/stats", timeout=10)
        if resp.status_code >= 300:
            return {}
        return resp.json().get("containers", {})
    except Exception as e:                        # noqa: BLE001
        log.warning("noc: stats fetch via proxy failed: %s", e)
        return {}


def _restart_warranted(svc: dict, up: bool, readiness: str,
                       deps_stable: bool = True) -> bool:
    """Whether the agent's CURRENT check results justify attempting a restart.
    A liveness failure always does. A staleness signal only does for agents
    with an enforced write cadence (restart_on_staleness) AND when the shared
    dependencies are confirmed healthy across consecutive cycles -- a flapping
    dependency masquerading as agent staleness must not restart the agent
    (FIXED 2026-08-15: that exact pattern fired three restarts in one hour
    and locked Quant Paper). Pure -- testable."""
    if not up:
        return True
    if readiness != "stale":
        return False
    if not svc.get("restart_on_staleness"):
        return False
    return deps_stable


def _check_healthy(svc: dict, up: bool, readiness: str) -> bool:
    """Whether this cycle counts as healthy for the 7-day uptime figure.
    Idle (stale but not enforced_cadence -- e.g. Study Platform simply having
    no users) counts healthy, the same way a skipped check (grace period,
    market closed) already does: the agent itself is fine either way.

    Keyed off enforced_cadence, NOT restart_on_staleness (ADDED 2026-08-17):
    Quant Live has a real enforced cadence -- its staleness is a genuine
    fault worth counting against uptime -- but must never auto-restart, so
    the two fields diverge for it. Using restart_on_staleness here would have
    scored Quant Live's staleness as "idle" and its uptime as artificially
    perfect, the same class of mistake already fixed once for Study
    Platform's uptime, just from the opposite direction."""
    if not up:
        return False
    if readiness in ("ok", "skipped", "n/a"):
        return True
    return readiness == "stale" and not svc.get("enforced_cadence")


def _restart_count(state: dict, name: str, now: dt.datetime) -> int:
    window = now.timestamp() - _RESTART_WINDOW_SEC
    return sum(1 for ts in state.get("restarts", {}).get(name, []) if ts >= window)


def _restart_eligible(svc: dict, now: dt.datetime, state: dict,
                      compliance: dict[str, list[str]] | None = None,
                      liveness_failure: bool = False) -> bool:
    """Permission gates for auto-restart (unhealthy/blocked gating lives in
    refresh_health, which has the fresh check results). Quarantined agents --
    manually paused, or compliance-critical (an OVERDUE rule naming them) --
    are never auto-restarted.

    The NYSE-session gate (any agent with market_hours_only=True -- ADDED
    2026-08-17, replacing a hardcoded "is this literally Quant Paper" name
    check now that Quant Live trades the same session) applies ONLY to a
    staleness-triggered restart (no new data is expected outside market
    hours, so there's nothing to fix) -- NOT to a genuine liveness failure
    (FIXED 2026-08-17: this gate previously blocked ALL restarts for Quant
    Paper outside market hours, including a real hung/dead process. A
    container that hung Saturday sat unreachable the entire weekend, since
    nyse_session_open() is never true then, and still hadn't recovered by
    Monday's pre-market check -- only a manual restart fixed it. A dead
    process is exactly as dead whether or not the market happens to be open,
    and leaving it dead until the open is strictly worse, not more
    cautious.)"""
    if svc["restart"] != "auto_heal" or not svc.get("container"):
        return False
    if svc["name"] in state.get("locks", {}):
        return False
    if _quarantine_blocks_restart(_quarantine_reason(svc["name"], state, compliance or {}),
                                  liveness_failure):
        return False
    if not liveness_failure and svc.get("market_hours_only") and not nyse_session_open(now):
        return False
    restarts = state.get("restarts", {}).get(svc["name"], [])
    if restarts and now.timestamp() - restarts[-1] < _RESTART_GRACE_SEC:
        return False
    return True


def _lock_agent(state: dict, name: str, now: dt.datetime) -> None:
    state.setdefault("locks", {})[name] = now.isoformat()
    # Strike bookkeeping (Phase 5 A1): locks tied together by an AUTO-unlock
    # within 6h are the same underlying failure repeating -- strikes escalate
    # (cooldown doubles, 2nd strike = sticky manual-only). An operator's
    # manual Clear resets strikes entirely (clear_lock); a lock arriving >6h
    # after the last auto-unlock is treated as a fresh problem.
    meta = state.setdefault("lock_meta", {}).setdefault(name, {})
    last_auto = meta.get("last_auto_unlock")
    within_window = (last_auto is not None
                     and (now.timestamp() - last_auto) < _LOCK_STICKY_WINDOW_SEC)
    meta["strikes"] = (meta.get("strikes", 0) + 1) if within_window else 1
    meta["sticky"] = meta["strikes"] >= 2
    meta["locked_at"] = now.isoformat()
    _add_incident(state, name, "locked",
                  outcome=f"auto-heal disabled (strike {meta['strikes']}"
                          + (", sticky" if meta["sticky"] else "") + ")")
    if _send_lock_alert(name):
        _add_incident(state, name, "alert sent", outcome="telegram", detail="lock alert")
    else:
        # The one notification that exists to say "auto-heal just disabled
        # itself on a live agent" must not vanish silently (FIXED 2026-08-15:
        # the Quant Paper lock alert recorded "telegram failed" and nothing
        # surfaced that anywhere except a log field). Queue it for automatic
        # retry each health cycle, and surface it as a dashboard banner until
        # it lands or is dismissed.
        state.setdefault("pending_alerts", {})[name] = {"kind": "lock alert",
                                                        "ts": now.isoformat()}
        _add_incident(state, name, "alert sent", outcome="telegram failed",
                      detail="lock alert queued for retry -- see dashboard banner")


# ---- Phase 5 A1: supervised auto-unlock with escalation ---------------------
# The manual-only lock protected against a multi-operator failure mode
# ("someone carelessly clicks clear") while taxing the only person who can
# clear it -- a flapping dependency at 2am left the agent dead until morning
# even though the dependency had recovered at 2:20. New model: self-recover
# with guardrails, keep the human informed.
_LOCK_BASE_COOLDOWN_SEC = 3600      # first strike: supervised recovery after 1h
_LOCK_STICKY_WINDOW_SEC = 6 * 3600  # re-lock within 6h of an auto-unlock escalates


def _auto_unlock_decision(meta: dict, now: dt.datetime, deps_stable: bool) -> str | None:
    """Decision for one locked agent this cycle: 'unlock' | 'wait-deps' | None.

    None        = cooldown not elapsed yet, no usable metadata, or STICKY
                  (2nd+ strike within the window -- restarts demonstrably
                  aren't healing; that is exactly what the lock exists to
                  stop, so only a human proceeds).
    'unlock'    = cooldown elapsed AND dependencies confirmed healthy for 2+
                  consecutive cycles (the same flapping-proof hysteresis that
                  gates staleness restarts) -- safe to attempt one supervised
                  recovery.
    'wait-deps' = cooldown elapsed but dependencies still down -- stay locked,
                  re-evaluate next cycle; unlocking into a known-down
                  Supabase/LLM API would just manufacture another restart.
    Pure -- testable without state files."""
    if not meta or meta.get("sticky") or not meta.get("locked_at"):
        return None
    locked_at = dt.datetime.fromisoformat(meta["locked_at"])
    if locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=dt.timezone.utc)
    cooldown = _LOCK_BASE_COOLDOWN_SEC * (2 ** (meta.get("strikes", 1) - 1))
    if (now - locked_at).total_seconds() < cooldown:
        return None
    return "unlock" if deps_stable else "wait-deps"


def _send_lock_alert(name: str) -> bool:
    """Send the lock Telegram alert, retrying once after a short pause -- the
    2026-08-14 failure was a transient network blip (the next alert eight
    seconds later landed), so one immediate retry would have caught it."""
    msg = (f"{name} locked: {_RESTART_LOCK_COUNT} restarts within the last hour "
           f"-- auto-unlock will be attempted after a cooldown, or clear it from "
           f"the dashboard / reply /unlock")
    if alerts.send_telegram(msg, tag="NOC", emoji="\U0001f6a8"):
        return True
    time.sleep(5)
    return alerts.send_telegram(msg, tag="NOC", emoji="\U0001f6a8")


def _retry_pending_alerts(state: dict) -> None:
    """Best-effort resend of failed lock alerts, once per health cycle; clears
    the pending entry on success."""
    for name in list(state.get("pending_alerts", {})):
        if alerts.send_telegram(
                f"{name} locked: {_RESTART_LOCK_COUNT} restarts within the last hour "
                f"-- clear the lock from the dashboard",
                tag="NOC", emoji="\U0001f6a8"):
            del state["pending_alerts"][name]
            _add_incident(state, name, "alert sent", outcome="telegram (retry)",
                          detail="lock alert")
        else:
            break  # still failing -- try again next cycle


def get_pending_alerts() -> dict:
    """Failed lock alerts still awaiting delivery -- drives the dashboard
    banner so a failed push is never the user's only warning."""
    return dict(_load_state().get("pending_alerts", {}))


def dismiss_pending() -> None:
    """UI action: acknowledge the delivery-failure banner. Logged to the
    incident log so the dismissal is attributable."""
    with _STATE_LOCK:
        state = _load_state()
        pending = state.pop("pending_alerts", {})
        for name in pending:
            _add_incident(state, name, "alert sent", outcome="dismissed by user",
                          detail="lock alert banner")
        _save_state(state)


def clear_lock(name: str) -> None:
    """UI action: unlock an auto-heal agent and reset its restart window so the
    cooldown doesn't immediately re-lock it. Logged to the incident log.
    Operator override also resets strike escalation -- the human has said
    'it's fine', so the next lock starts fresh at strike 1."""
    with _STATE_LOCK:
        state = _load_state()
        if name in state.get("locks", {}):
            del state["locks"][name]
            state.get("restarts", {}).pop(name, None)
            _add_incident(state, name, "unlocked", outcome="restart window reset")
        state.get("lock_meta", {}).pop(name, None)
        state.get("pending_alerts", {}).pop(name, None)  # stale once unlocked
        _save_state(state)
    if name in _STATUS_CACHE:
        _STATUS_CACHE[name]["locked"] = False


# ---- quarantine (Task D, Phase 2.3) -----------------------------------------
# "Safe quarantine": an operator decision, never automatic. Quarantine means
# (a) the container is PAUSED via the restart proxy (the caller does that) and
# (b) the NOC stops auto-restarting the agent -- a paused container reads as
# "down" to liveness probes, and restarting it would fight the quarantine.
# A compliance-critical agent (an OVERDUE rule naming it) is also treated as
# quarantined for restart purposes: you don't bounce a system while its
# compliance posture is red, even if the container is technically healthy.

def _quarantine_reason(name: str, state: dict,
                       compliance: dict[str, list[str]]) -> str | None:
    """None | 'manual' | 'compliance-auto' | 'compliance' -- why an agent's
    restart is suppressed. A pause recorded in state (operator or auto) wins
    and preserves its reason; otherwise any OVERDUE rule naming the agent
    counts as 'compliance' (restart suppression even without a pause)."""
    entry = state.get("quarantined", {}).get(name)
    if entry:
        return entry.get("reason", "manual")
    if compliance.get(name):
        return "compliance"
    return None


def _quarantine_blocks_restart(reason: str | None, liveness_failure: bool) -> bool:
    """Whether a quarantine reason should hold back THIS restart attempt.
    'manual' and 'compliance-auto' both mean the container was actually
    paused (an operator, or policy, took a deliberate action) -- restarting
    would directly fight that, so both always block, liveness failure or not.
    A bare 'compliance' reason means an OVERDUE rule names this agent but
    nothing was ever paused (FIXED 2026-08-17: this used to block a genuine
    liveness failure too, for a reason that might have nothing to do with the
    agent's technical health -- e.g. an unrelated paperwork deadline going
    overdue shouldn't leave a hung process unrepaired). A staleness-triggered
    restart still respects a bare 'compliance' flag -- bouncing a technically-
    reachable agent while its compliance posture is red is exactly the
    caution that flag exists for."""
    if not reason:
        return False
    if reason == "compliance" and liveness_failure:
        return False
    return True


def quarantine_agent(name: str, reason: str = "manual") -> None:
    """Operator-initiated quarantine. The container pause is done by the
    caller through the restart proxy; this records the state so the NOC stops
    auto-restarting the agent. Logged to the incident log."""
    with _STATE_LOCK:
        state = _load_state()
        state.setdefault("quarantined", {})[name] = {
            "reason": reason,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        _add_incident(state, name, "quarantine", outcome=reason,
                      detail="operator paused container")
        _save_state(state)
    if name in _STATUS_CACHE:
        _STATUS_CACHE[name]["quarantined"] = "manual"


def unquarantine_agent(name: str) -> bool:
    """Lift a manual quarantine (the caller also unpauses the container)."""
    with _STATE_LOCK:
        state = _load_state()
        if state.get("quarantined", {}).pop(name, None):
            _add_incident(state, name, "quarantine-lifted",
                          outcome="operator resumed container")
            _save_state(state)
            return True
    if name in _STATUS_CACHE:
        _STATUS_CACHE[name]["quarantined"] = False
    return False


_QUARANTINE_RECONCILE_STREAK = 2   # consecutive reachable cycles before a stale flag self-clears


def _reconcile_quarantine(state: dict, name: str, up: bool) -> bool:
    """Self-heal a stale 'quarantined' flag: if an agent marked quarantined
    is actually reachable for _QUARANTINE_RECONCILE_STREAK consecutive
    cycles, the container was almost certainly unpaused through some path
    other than resume_agent() -- a proxy call whose response was lost even
    though the underlying unpause succeeded, a manual `docker unpause`
    outside the dashboard, anything. Mutates `state` in place; returns True
    if this call cleared the flag. Pure aside from that mutation -- no file
    I/O, so it's directly testable.

    FIXED 2026-08-18: previously nothing ever re-checked a quarantine flag
    against reality. A resume attempt reported "proxy unreachable", the
    container became reachable again a few minutes later regardless, and
    the card kept showing "quarantined (paused)" indefinitely -- the only
    way out was clicking Resume again and hoping. Deliberately requires TWO
    consecutive reachable cycles (4 min), not one, so a single flaky probe
    can't prematurely clear a quarantine that's still genuinely in effect.

    Only reconciles 'manual' quarantines. A 'compliance-auto' pause is
    policy-driven -- if the container answers again, that's not proof the
    underlying compliance issue is resolved, so it stays paused until the
    rule is actually marked complied (see app.py::_mark_complied(), which
    already handles that resume path deliberately, not via this reconciler)."""
    entry = state.get("quarantined", {}).get(name)
    streaks = state.setdefault("quarantine_up_streak", {})
    if not entry or entry.get("reason") == "compliance-auto":
        streaks.pop(name, None)
        return False
    if not up:
        streaks[name] = 0
        return False
    streaks[name] = streaks.get(name, 0) + 1
    if streaks[name] < _QUARANTINE_RECONCILE_STREAK:
        return False
    del state["quarantined"][name]
    del streaks[name]
    _add_incident(state, name, "quarantine-reconciled",
                  outcome="container reachable again -- stale quarantine flag cleared",
                  detail="not resumed via this dashboard's Resume action")
    return True


# ---- quarantine passcode gate -----------------------------------------------
# Both manual container actions -- Quarantine (pause) and Resume -- go through
# check_quarantine_passcode() before the caller is allowed to touch the
# container. Every attempt is logged to the incident log, success or failure,
# same as everything else in this module (FIXED 2026-08-17: previously
# neither action required anything at all beyond a confirm-dialog click, on a
# dashboard the open internet can reach). Repeated failures against the same
# agent lock further attempts out and page Telegram, mirroring the existing
# 3-restarts-in-an-hour cooldown pattern exactly.

def passcode_configured() -> bool:
    """Whether NOC_ACTION_PASSCODE is set -- the UI disables both manual
    actions entirely when this is False, rather than silently letting them
    through unauthenticated."""
    return bool(_ACTION_PASSCODE)


def is_auth_locked(name: str) -> bool:
    """Whether repeated failed passcode attempts have locked out further
    manual actions for this agent. Distinct from the restart cooldown lock
    (state key `locks`) -- this one is about who's allowed to try, not
    whether auto-heal should keep retrying."""
    return name in _load_state().get("auth_locks", {})


def clear_auth_lock(name: str) -> None:
    """UI action: clear a repeated-failed-passcode lockout. Logged."""
    with _STATE_LOCK:
        state = _load_state()
        if state.get("auth_locks", {}).pop(name, None) is not None:
            state.get("auth_failures", {}).pop(name, None)
            _add_incident(state, name, "auth-unlocked", outcome="operator cleared lockout")
            _save_state(state)


def _passcode_attempt(state: dict, name: str, entered: str, now: dt.datetime) -> tuple[bool, str]:
    """Pure decision + in-memory state mutation for one passcode attempt --
    no file I/O, so it's directly testable (matching every other interesting
    function in this module). See check_quarantine_passcode() for the public,
    file-backed wrapper. Mutates `state` in place (failure list, lock,
    incidents); the caller is responsible for persisting it."""
    if name in state.get("auth_locks", {}):
        _add_incident(state, name, "quarantine-auth-failed",
                      outcome="blocked -- already auth-locked",
                      detail="attempt made while locked out")
        return False, "locked out from too many failed attempts -- clear the lock first"

    if not _ACTION_PASSCODE:
        _add_incident(state, name, "quarantine-auth-failed",
                      outcome="no passcode configured", detail="action blocked")
        return False, "passcode not configured -- action disabled"

    if hmac.compare_digest(entered or "", _ACTION_PASSCODE):
        return True, ""

    failures = state.setdefault("auth_failures", {}).setdefault(name, [])
    failures.append(now.timestamp())
    window = now.timestamp() - _AUTH_FAIL_WINDOW_SEC
    state["auth_failures"][name] = [ts for ts in failures if ts >= window]
    fail_count = len(state["auth_failures"][name])
    _add_incident(state, name, "quarantine-auth-failed",
                  outcome=f"wrong passcode ({fail_count}/{_AUTH_FAIL_LIMIT})")

    if fail_count >= _AUTH_FAIL_LIMIT:
        state.setdefault("auth_locks", {})[name] = now.isoformat()
        _add_incident(state, name, "auth-locked",
                      outcome=f"{_AUTH_FAIL_LIMIT} failed attempts within "
                              f"{_AUTH_FAIL_WINDOW_SEC // 60} min")
        alerts.send_telegram(
            f"\U0001f512 {name}: {_AUTH_FAIL_LIMIT} failed quarantine-passcode attempts "
            f"in {_AUTH_FAIL_WINDOW_SEC // 60} min -- manual actions locked, "
            f"clear from the dashboard if this was you",
            tag="NOC", emoji="\U0001f512")

    return False, "wrong passcode"


def check_quarantine_passcode(name: str, entered: str) -> tuple[bool, str]:
    """Verify a passcode attempt for a manual Quarantine/Resume action
    against `name`. Returns (allowed, reason) -- reason is empty on success,
    otherwise a short string safe to show the operator. Every attempt is
    logged to the incident log regardless of outcome, since a failed attempt
    is itself a signal worth keeping, not just a UI toast that vanishes.

    Fails closed: no configured passcode means nothing is ever allowed
    through, logged the same as a wrong one. Already-locked-out agents are
    rejected before the passcode is even checked, and that rejection is
    logged too (continued probing after a lockout is worth seeing)."""
    now = dt.datetime.now(dt.timezone.utc)
    with _STATE_LOCK:
        state = _load_state()
        allowed, reason = _passcode_attempt(state, name, entered, now)
        _save_state(state)
        return allowed, reason


# ---- incidents -------------------------------------------------------------

def _add_incident(state: dict, agent: str, event: str, outcome: str = "",
                  detail: str = "") -> None:
    incidents = state.setdefault("incidents", [])
    incidents.append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "agent": agent, "event": event, "outcome": outcome, "detail": detail,
    })
    del incidents[:-_INCIDENT_LIMIT]              # keep the newest 50


def get_incidents(limit: int = 50) -> list[dict]:
    """Most recent incidents first -- for the dashboard's incident-log table."""
    return list(reversed(_load_state().get("incidents", [])))[:limit]


# ---- uptime ----------------------------------------------------------------

def _record_check(state: dict, name: str, healthy: bool, now: dt.datetime) -> None:
    checks = state.setdefault("checks", {}).setdefault(name, {})
    day = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    entry = checks.setdefault(day, {"ok": 0, "fail": 0})
    entry["ok" if healthy else "fail"] += 1
    cutoff = (now.astimezone(dt.timezone.utc) - dt.timedelta(days=_UPTIME_DAYS)
              ).strftime("%Y-%m-%d")
    for stale in [d for d in checks if d < cutoff]:
        del checks[stale]
    _record_slot(state, name, healthy, now)


def _record_slot(state: dict, name: str, healthy: bool, now: dt.datetime) -> None:
    """Fold this cycle's result into the current 6-hour slot (see _SLOT_SEC).
    Mutates `state`; persisted by the caller's _save_state. Pure aside from
    that mutation."""
    slot_start = int(now.timestamp()) // _SLOT_SEC * _SLOT_SEC
    slots = state.setdefault("check_slots", {}).setdefault(name, [])
    if not slots or slots[-1]["start"] != slot_start:
        slots.append({"start": slot_start, "ok": 0, "fail": 0})
        del slots[:-_SLOT_COUNT]
    slots[-1]["ok" if healthy else "fail"] += 1


def uptime_slots(name: str) -> list[str]:
    """The trailing 28 six-hour slots for one agent, OLDEST FIRST, each
    'ok' | 'fail' | 'none' -- the data behind the card's uptime strip.
    Reads only from the state file; render-safe (no network)."""
    slots_by_start = {s["start"]: s for s in _load_state().get("check_slots", {}).get(name, [])}
    current_start = int(time.time()) // _SLOT_SEC * _SLOT_SEC
    out = []
    for i in range(_SLOT_COUNT):
        start = current_start - (_SLOT_COUNT - 1 - i) * _SLOT_SEC
        entry = slots_by_start.get(start)
        if not entry or (entry["ok"] + entry["fail"]) == 0:
            out.append("none")
        else:
            out.append("fail" if entry["fail"] > 0 and entry["ok"] == 0
                       else ("mixed" if entry["fail"] else "ok"))
    return out


def _uptime_7d(state: dict, name: str, now: dt.datetime | None = None) -> float | None:
    """Percentage of checks within the last 7 days that came back healthy, or
    None if no checks recorded yet. `now` (aware) anchors the window -- pass
    it for testability."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now.astimezone(dt.timezone.utc) - dt.timedelta(days=_UPTIME_DAYS)
              ).strftime("%Y-%m-%d")
    checks = {d: e for d, e in state.get("checks", {}).get(name, {}).items() if d >= cutoff}
    ok = sum(e["ok"] for e in checks.values())
    total = ok + sum(e["fail"] for e in checks.values())
    return round(ok / total * 100, 1) if total else None


# ---- Phase 5 A3: maintenance window -----------------------------------------
# Deploys bounce containers outside this dashboard's knowledge; each bounce
# reads as a liveness failure and feeds the very restart/lock machinery an
# operator is trying to keep quiet. A maintenance window suppresses ACTIONS
# (restarts, alert_only alerts, blocked-by incidents) and uptime recording --
# "no data" during planned work must not score as downtime -- while probes
# still run so the cards stay honest afterwards.

def set_maintenance(minutes: int, scope: str = "all") -> None:
    """Arm a maintenance window. scope 'all' covers every agent; otherwise a
    comma-separated agent-name list (Telegram: /mute 30 quant-paper)."""
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    with _STATE_LOCK:
        state = _load_state()
        state["maintenance"] = {"until": until.isoformat(), "scope": scope}
        _save_state(state)


def clear_maintenance() -> None:
    with _STATE_LOCK:
        state = _load_state()
        if state.pop("maintenance", None) is not None:
            _add_incident(state, "(global)", "maintenance-cleared",
                          outcome="monitoring resumed")
            _save_state(state)


def maintenance_active(name: str | None = None,
                       now: dt.datetime | None = None) -> dict | None:
    """The active maintenance entry covering `name` (or any, when None), or
    None. Read-only; safe from render paths."""
    entry = _load_state().get("maintenance")
    if not entry:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        until = dt.datetime.fromisoformat(entry["until"])
    except (KeyError, ValueError):
        return None
    if now >= until:
        return None
    scope = entry.get("scope", "all")
    if scope != "all" and name and name not in scope.split(","):
        return None
    return entry


def maintenance_remaining_min() -> int | None:
    """Whole minutes left in the global window, for the dashboard banner."""
    entry = maintenance_active()
    if not entry:
        return None
    until = dt.datetime.fromisoformat(entry["until"])
    return max(1, int((until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60))


# ---- state file (same pattern as alerts.py) --------------------------------

def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state))


# ---- the monitoring cycle --------------------------------------------------

def refresh_health() -> None:
    """One full monitoring cycle. Blocking -- call via asyncio.to_thread from
    async code, never from a page-render path. Never raises: a single bad
    cycle must not kill the background loop (previous cycle's status stays
    visible and the next cycle retries)."""
    try:
        _refresh_health()
    except Exception:                             # noqa: BLE001
        log.exception("noc: monitoring cycle failed")


def _refresh_health() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with _STATE_LOCK:
        state = _load_state()
        monitored = [s for s in services.SERVICES if s.get("monitor")]

        with ThreadPoolExecutor(max_workers=max(len(monitored), 1)) as pool:
            up_map = dict(zip([s["name"] for s in monitored], pool.map(_liveness, monitored)))

        dep_results = _dependency_probe_results()
        dep_streaks = {dep: _update_dep_streak(dep, ok) for dep, ok in dep_results.items()}
        # confirmed down = 2+ consecutive failing cycles (flapping-proof badge);
        # stable = every dependency confirmed healthy for 2+ consecutive cycles
        # (required before a staleness-triggered restart may proceed).
        confirmed_down = [dep for dep, s in dep_streaks.items() if _dep_confirmed_down(s)]
        deps_stable = all(_dep_confirmed_healthy(s) for s in dep_streaks.values())

        freshness_agents = [s for s in monitored
                            if s.get("project_tag") or s.get("freshness_table")]
        with ThreadPoolExecutor(max_workers=max(len(freshness_agents), 1)) as pool:
            last_write_map = dict(zip(
                [s["name"] for s in freshness_agents],
                pool.map(_latest_write_safe, freshness_agents)))

        prev = {name: dict(_STATUS_CACHE.get(name, {})) for name in up_map}
        compliance = governance.compliance_health()  # agent -> OVERDUE rule names
        auto_targets = governance.auto_quarantine_targets()  # agent -> {rule, rule_id}
        stats_map = _container_stats_map()  # container name -> {usage_bytes, limit_bytes}

        for svc in monitored:
            name = svc["name"]
            up = up_map[name]
            locked = name in state.get("locks", {})
            muted = maintenance_active(name, now)  # Phase 5 A3
            restarts = state.get("restarts", {}).get(name, [])
            last_restart = restarts[-1] if restarts else None
            if svc.get("quarantinable"):
                _reconcile_quarantine(state, name, up)
            quarantine = _quarantine_reason(name, state, compliance)

            readiness, detail = "n/a", ""
            if svc.get("project_tag") or svc.get("freshness_table"):
                readiness, detail = _readiness(svc, now, last_restart,
                                               last_write_map.get(name))

            # Memory reading is opt-in per agent: only meaningful for agents
            # with a real Docker container, and only reachable at all for
            # ones on the restart-proxy's allow-list (ADDED 2026-09-04).
            mem_stats = stats_map.get(svc.get("container") or "")
            memory_mb = round(mem_stats["usage_bytes"] / 1_048_576, 1) if mem_stats else None
            # An uncapped container's cgroup limit doesn't read back as null
            # or a platform sentinel -- it reports the WSL2 VM's own total
            # memory (~15.5GB here, confirmed live), since that's the
            # effective ceiling with no per-container cgroup limit set. No
            # real per-container cap under this spec would come anywhere
            # near host total, so treat anything over 4GB as "no limit set"
            # rather than display the host ceiling as if it were intentional.
            limit_bytes = mem_stats.get("limit_bytes") if mem_stats else None
            memory_limit_mb = (round(limit_bytes / 1_048_576, 1)
                                if limit_bytes and limit_bytes < 4_294_967_296 else None)

            unhealthy = (not up) or (readiness == "stale")
            blocked_by = list(confirmed_down) if (unhealthy and confirmed_down) else []
            was_blocked = bool(prev.get(name, {}).get("blocked_by"))
            if blocked_by and not was_blocked and not muted:
                _add_incident(state, name, "blocked-by-dependency",
                              outcome=", ".join(blocked_by),
                              detail="restart suppressed while dependency is down")
            was_quarantined = prev.get(name, {}).get("quarantined")

            # Task 4 (Phase 3.2): policy-driven automated isolation -- per-rule
            # opt-in via governance_rules.auto_action='quarantine'. An OVERDUE
            # rule targeting this agent pauses its container ONCE (through the
            # proxy) unless a pause is already recorded. The gate is "not
            # already paused/recorded", NOT "not compliance-quarantined" -- the
            # same OVERDUE rule makes this agent compliance-quarantined for
            # restart purposes, and that is exactly when the pause should fire.
            # Guards: only quarantinable agents with a container; never a
            # market_hours_only agent during its own session (a pause
            # mid-session is the operator's call, not a rule's -- ADDED
            # 2026-08-17: generalized off the field rather than hardcoding a
            # second agent name now that Quant Live is also a trading agent).
            auto_target = auto_targets.get(name)
            if (name not in state.get("quarantined", {}) and auto_target
                    and svc.get("quarantinable") and svc.get("container")):
                if svc.get("market_hours_only") and nyse_session_open(now):
                    _add_incident(state, name, "auto-quarantine skipped",
                                  outcome="market hours", detail=auto_target["rule"])
                elif _proxy_action("pause", svc["container"]):
                    # Inline the record: the whole cycle already runs inside
                    # _STATE_LOCK, so calling quarantine_agent() (which acquires
                    # it again) would DEADLOCK -- the pause fired but the record
                    # never saved and the cycle hung (FIXED 2026-08-16). The
                    # cycle's final _save_state() persists this.
                    state.setdefault("quarantined", {})[name] = {
                        "reason": "compliance-auto",
                        "ts": now.isoformat(),
                    }
                    _add_incident(state, name, "quarantine", outcome="compliance-auto",
                                  detail=f"auto-paused by policy: {auto_target['rule']}")
                    alerts.send_telegram(
                        f"\U0001f6d1 {name} auto-quarantined: '{auto_target['rule']}' "
                        f"is OVERDUE -- container paused by policy",
                        tag="NOC", emoji="\U0001f6d1")
                    quarantine = "compliance-auto"
                else:
                    _add_incident(state, name, "auto-quarantine failed",
                                  outcome="pause error", detail=auto_target["rule"])

            if quarantine and not was_quarantined and quarantine != "compliance-auto":
                _add_incident(state, name, "quarantine", outcome=quarantine,
                              detail="restart suppressed (compliance or manual)")

            # Phase 5 A1: supervised auto-unlock. Cooldown elapsed + deps
            # confirmed stable -> clear, alert, and attempt ONE recovery
            # restart. Deliberately bypasses _restart_eligible's grace checks:
            # this IS the supervised recovery, not another blind auto-heal.
            lock_info = None
            if locked:
                meta = state.get("lock_meta", {}).get(name, {})
                decision = _auto_unlock_decision(meta, now, deps_stable)
                strikes = meta.get("strikes", 1)
                locked_at_iso = meta.get("locked_at")
                if meta.get("sticky"):
                    lock_info = {"sticky": True, "strikes": strikes,
                                 "unlocks_in_min": None}
                elif locked_at_iso:
                    locked_at = dt.datetime.fromisoformat(locked_at_iso)
                    if locked_at.tzinfo is None:
                        locked_at = locked_at.replace(tzinfo=dt.timezone.utc)
                    cooldown_sec = _LOCK_BASE_COOLDOWN_SEC * (2 ** (strikes - 1))
                    remaining = max(0, cooldown_sec
                                    - (now - locked_at).total_seconds())
                    lock_info = {"sticky": False, "strikes": strikes,
                                 "unlocks_in_min": int(remaining // 60) + (1 if remaining % 60 else 0)}
                else:
                    # Legacy lock (created before strike bookkeeping existed):
                    # no known locked-at time -> can't schedule a supervised
                    # recovery; show it as needing the operator instead of
                    # crashing the whole monitoring cycle (found live 2026-08-26:
                    # the deployed state file held a pre-Phase-5 lock).
                    lock_info = {"sticky": True, "strikes": strikes,
                                 "unlocks_in_min": None}
                if decision == "unlock" and svc.get("container"):
                    del state["locks"][name]
                    state.get("restarts", {}).pop(name, None)
                    meta["last_auto_unlock"] = now.timestamp()
                    meta["locked_at"] = None
                    locked = False
                    _add_incident(state, name, "auto-unlocked",
                                  outcome=f"cooldown elapsed ({strikes} strike(s)), deps stable",
                                  detail="one supervised recovery restart follows")
                    alerts.send_telegram(
                        f"\u2705 {name} auto-unlocked after cooldown -- attempting "
                        f"one supervised recovery restart", tag="NOC", emoji="\u2705")
                    outcome = "ok" if _restart_container(svc["container"]) else "failed"
                    state.setdefault("restarts", {}).setdefault(name, []).append(now.timestamp())
                    window = now.timestamp() - _RESTART_WINDOW_SEC
                    state["restarts"][name] = [ts for ts in state["restarts"][name]
                                               if ts >= window]
                    _add_incident(state, name, "restarted", outcome=outcome,
                                  detail="supervised recovery after auto-unlock")
                    locked = name in state.get("locks", {})  # re-lock would need 3 fresh restarts

            if _restart_warranted(svc, up, readiness, deps_stable) \
                    and svc["restart"] == "auto_heal":
                if muted:
                    pass  # maintenance window: observe, don't act (A3)
                elif blocked_by:
                    pass  # circuit breaker: dependency confirmed down
                elif locked:
                    pass
                elif _quarantine_blocks_restart(quarantine, liveness_failure=not up):
                    pass  # quarantined: never bounce while red/manually paused
                    # (unless it's a bare compliance flag AND this is a genuine
                    # liveness failure -- see _quarantine_blocks_restart)
                elif _restart_eligible(svc, now, state, compliance, liveness_failure=not up):
                    outcome = "ok" if _restart_container(svc["container"]) else "failed"
                    state.setdefault("restarts", {}).setdefault(name, []).append(now.timestamp())
                    _add_incident(state, name, "restarted", outcome=outcome, detail="auto-heal")
                    # prune the rolling window
                    window = now.timestamp() - _RESTART_WINDOW_SEC
                    state["restarts"][name] = [ts for ts in state["restarts"][name] if ts >= window]
                    if _restart_count(state, name, now) >= _RESTART_LOCK_COUNT:
                        _lock_agent(state, name, now)
                        locked = True
            elif unhealthy and svc["restart"] == "alert_only" \
                    and not prev.get(name, {}).get("alerted_unhealthy"):
                if muted:
                    pass  # maintenance window: record, don't page (A3)
                # ADDED 2026-08-17: this used to always say "liveness check
                # failed" -- fine while every alert_only agent was liveness-
                # only, wrong the moment one (Quant Live) can also go
                # unhealthy purely from staleness while still reachable.
                elif not up:
                    reason = "liveness check failed"
                    incident_detail = "down alert"
                else:
                    reason = f"stale -- {detail or 'no recent data'}"
                    incident_detail = "staleness alert"
                ok = alerts.send_telegram(
                    f"{name} is unhealthy ({reason})", tag="NOC",
                    emoji="\U0001f6a8")
                _add_incident(state, name, "alert sent",
                              outcome="telegram" if ok else "telegram failed",
                              detail=incident_detail)

            # uptime: "healthy" = liveness ok AND readiness not a real fault.
            # Idle (stale but not enforced_cadence, e.g. Study Platform
            # simply having no users) counts healthy, the same way a skipped
            # check (grace / market closed) already does.
            healthy = _check_healthy(svc, up, readiness)
            if muted:
                pass  # planned work: "no data" must not score as downtime (A3)
            else:
                _record_check(state, name, healthy, now)

            _STATUS_CACHE[name] = {
                "up": up,
                "readiness": readiness,
                "readiness_detail": detail,
                "blocked_by": blocked_by if not muted else [],
                "locked": locked,
                "lock_info": lock_info,
                "muted": bool(muted),
                "quarantined": quarantine,  # None | 'manual' | 'compliance'
                "last_write": last_write_map.get(name),
                "checked_at": time.time(),
                "uptime_7d": _uptime_7d(state, name, now),
                "memory_mb": memory_mb,
                "memory_limit_mb": memory_limit_mb,
                "alerted_unhealthy": (prev.get(name, {}).get("alerted_unhealthy", False)
                                       or unhealthy) if svc["restart"] == "alert_only" else False,
            }

        _retry_pending_alerts(state)
        _save_state(state)


def get_status(name: str) -> dict | None:
    return _STATUS_CACHE.get(name)


# ---- selftest (stdlib only) -------------------------------------------------

def _selftest() -> None:
    import unittest

    class NocSelftest(unittest.TestCase):
        def test_dst_transition_same_et_wallclock(self):
            # Mon 2026-03-09 (EDT, UTC-4): 13:30 UTC == 09:30 ET -> open.
            self.assertTrue(nyse_session_open(dt.datetime(2026, 3, 9, 13, 30, tzinfo=dt.timezone.utc)))
            # Mon 2026-02-09 (EST, UTC-5): 14:30 UTC == 09:30 ET -> open.
            self.assertTrue(nyse_session_open(dt.datetime(2026, 2, 9, 14, 30, tzinfo=dt.timezone.utc)))
            # Mon 2026-11-02 (EST after fall-back): 14:30 UTC == 09:30 ET -> open.
            self.assertTrue(nyse_session_open(dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.timezone.utc)))

        def test_session_bounds(self):
            t = lambda h, m: dt.datetime(2026, 3, 9, h, m, tzinfo=dt.timezone.utc)  # EDT day
            self.assertTrue(nyse_session_open(t(13, 30)))     # 09:30 ET
            self.assertFalse(nyse_session_open(t(13, 29)))    # 09:29 ET
            self.assertTrue(nyse_session_open(t(20, 0)))      # 16:00 ET
            self.assertFalse(nyse_session_open(t(20, 1)))     # 16:01 ET

        def test_weekend_closed(self):
            # Sat 2026-03-07 15:00 UTC = 10:00 EST -> closed (weekend).
            self.assertFalse(nyse_session_open(dt.datetime(2026, 3, 7, 15, 0, tzinfo=dt.timezone.utc)))

        def test_holiday_closed(self):
            try:
                import pandas_market_calendars  # noqa: F401
            except ImportError:
                self.skipTest("pandas_market_calendars not installed")
            # Thu 2026-01-01 15:00 UTC = 10:00 EST -> closed (New Year's Day).
            self.assertFalse(nyse_session_open(dt.datetime(2026, 1, 1, 15, 0, tzinfo=dt.timezone.utc)))

        def test_readiness(self):
            svc = {"name": "X", "project_tag": "x", "freshness_sec": 3600}
            now = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            fresh = (now - dt.timedelta(minutes=30)).isoformat()
            stale = (now - dt.timedelta(hours=2)).isoformat()
            self.assertEqual(_readiness(svc, now, None, fresh)[0], "ok")
            self.assertEqual(_readiness(svc, now, None, stale)[0], "stale")
            self.assertEqual(_readiness(svc, now, None, None)[0], "stale")
            # grace: last restart 60s ago -> skipped
            self.assertEqual(_readiness(svc, now, now.timestamp() - 60, stale)[0], "skipped")
            # market_hours_only agent outside market hours -> skipped (Sat)
            qp = {**svc, "name": QUANT_PAPER, "market_hours_only": True}
            sat = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
            self.assertEqual(_readiness(qp, sat, None, stale)[0], "skipped")
            # a stale agent WITHOUT market_hours_only is not skipped on a
            # weekend -- proves the exception is field-driven, not name-driven
            # (Event Radar has no session; its staleness never gets a pass).
            no_session = {**svc, "name": "Event Radar"}
            self.assertEqual(_readiness(no_session, sat, None, stale)[0], "stale")
            # freshness_table-only agent (Study Platform): readiness is still
            # evaluated -- the source override is what changed, not the gate
            st = {"name": "S", "freshness_table": "answer_log", "freshness_sec": 43200}
            stale12 = (now - dt.timedelta(hours=13)).isoformat()
            self.assertEqual(_readiness(st, now, None, fresh)[0], "ok")
            self.assertEqual(_readiness(st, now, None, stale12)[0], "stale")

        def test_lock_after_three_restarts(self):
            state = {"restarts": {"X": []}}
            now = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            svc = {"name": "X", "restart": "auto_heal", "container": "x"}
            self.assertTrue(_restart_eligible(svc, now, state))
            state["restarts"]["X"] = [now.timestamp()] * 3
            self.assertEqual(_restart_count(state, "X", now), 3)
            # a locked agent is never restart-eligible
            state["locks"] = {"X": now.isoformat()}
            self.assertFalse(_restart_eligible(svc, now, state))
            # unlock clears the eligibility block
            del state["locks"]["X"]
            state["restarts"]["X"] = []
            self.assertTrue(_restart_eligible(svc, now, state))

        def test_market_hours_gate_applies_only_to_staleness(self):
            # FIXED 2026-08-17: a container that hung over a weekend was never
            # auto-restarted, because the NYSE gate blocked every restart
            # attempt for Quant Paper regardless of WHY it was being attempted.
            state = {"restarts": {}}
            sat = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)  # market closed
            svc = {"restart": "auto_heal", "container": "quant-dashboard-docker",
                   "name": QUANT_PAPER, "market_hours_only": True}
            # staleness-triggered (default liveness_failure=False): still held
            # for market hours, as intended -- no new data is expected anyway.
            self.assertFalse(_restart_eligible(svc, sat, state))
            # an agent WITHOUT market_hours_only is never held back by it --
            # proves the gate is field-driven, not a second hardcoded name.
            no_session = {**svc, "name": "Event Radar", "market_hours_only": False}
            self.assertTrue(_restart_eligible(no_session, sat, state))
            # a genuine liveness failure must be restart-eligible regardless --
            # a dead process is exactly as dead whether the market is open.
            self.assertTrue(_restart_eligible(svc, sat, state, liveness_failure=True))
            # during market hours, both paths are eligible as before.
            mon_open = dt.datetime(2026, 8, 17, 14, 0, tzinfo=dt.timezone.utc)  # 10:00 ET
            self.assertTrue(_restart_eligible(svc, mon_open, state))
            self.assertTrue(_restart_eligible(svc, mon_open, state, liveness_failure=True))

        def test_quarantine_reason(self):
            # no pause recorded, no overdue rule -> None
            self.assertIsNone(_quarantine_reason("A", {"quarantined": {}}, {}))
            # overdue rule without a pause -> compliance (restart suppressed)
            self.assertEqual(_quarantine_reason("A", {"quarantined": {}}, {"A": ["r"]}),
                             "compliance")
            # a recorded pause wins and preserves its reason
            state = {"quarantined": {"A": {"reason": "compliance-auto", "ts": "x"}}}
            self.assertEqual(_quarantine_reason("A", state, {"A": ["r"]}), "compliance-auto")
            state = {"quarantined": {"A": {"reason": "manual", "ts": "x"}}}
            self.assertEqual(_quarantine_reason("A", state, {"A": ["r"]}), "manual")
            # quarantined agents are never restart-eligible
            svc = {"name": "A", "restart": "auto_heal", "container": "a"}
            now = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            self.assertFalse(_restart_eligible(svc, now, {"quarantined": {"A": {"reason": "manual"}}},
                                               compliance={"A": ["r"]}))

        def test_quarantine_blocks_restart_scoping(self):
            # FIXED 2026-08-17: a bare 'compliance' flag (an overdue rule
            # names the agent, but nothing was ever actually paused) must not
            # hold back a genuine liveness failure -- an unrelated paperwork
            # deadline going overdue shouldn't leave a hung process unrepaired.
            self.assertFalse(_quarantine_blocks_restart(None, liveness_failure=False))
            self.assertFalse(_quarantine_blocks_restart(None, liveness_failure=True))
            self.assertFalse(_quarantine_blocks_restart("compliance", liveness_failure=True))
            self.assertTrue(_quarantine_blocks_restart("compliance", liveness_failure=False))
            # an ACTUAL pause -- manual or policy-driven -- always blocks,
            # liveness failure or not: restarting would directly fight a
            # deliberate action someone (or some rule) already took.
            self.assertTrue(_quarantine_blocks_restart("manual", liveness_failure=True))
            self.assertTrue(_quarantine_blocks_restart("manual", liveness_failure=False))
            self.assertTrue(_quarantine_blocks_restart("compliance-auto", liveness_failure=True))
            self.assertTrue(_quarantine_blocks_restart("compliance-auto", liveness_failure=False))
            # end to end through _restart_eligible: a bare compliance flag
            # lets a liveness-failure restart through, an actual pause never does.
            svc = {"name": "A", "restart": "auto_heal", "container": "a"}
            now = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            self.assertTrue(_restart_eligible(svc, now, {"quarantined": {}}, compliance={"A": ["r"]},
                                              liveness_failure=True))
            self.assertFalse(_restart_eligible(svc, now, {"quarantined": {}}, compliance={"A": ["r"]},
                                               liveness_failure=False))
            self.assertFalse(_restart_eligible(svc, now, {"quarantined": {"A": {"reason": "manual"}}},
                                               compliance={"A": ["r"]}, liveness_failure=True))

        def test_reconcile_quarantine(self):
            # FIXED 2026-08-18: a manual quarantine flag with no way to
            # notice the container became reachable again through some other
            # path (a proxy call whose response was lost, a manual `docker
            # unpause` outside the dashboard) stayed stuck forever.
            state = {"quarantined": {"A": {"reason": "manual", "ts": "x"}}}

            # not yet reachable: no change, no streak yet
            self.assertFalse(_reconcile_quarantine(state, "A", up=False))
            self.assertIn("A", state["quarantined"])

            # ONE reachable cycle is not enough -- avoids clearing a real
            # quarantine on a single flaky liveness probe
            self.assertFalse(_reconcile_quarantine(state, "A", up=True))
            self.assertIn("A", state["quarantined"])

            # a cycle back to unreachable resets the streak
            self.assertFalse(_reconcile_quarantine(state, "A", up=False))
            self.assertFalse(_reconcile_quarantine(state, "A", up=True))
            self.assertIn("A", state["quarantined"], "streak should have reset, not carried over")

            # TWO consecutive reachable cycles clears it and logs why
            self.assertTrue(_reconcile_quarantine(state, "A", up=True))
            self.assertNotIn("A", state["quarantined"])
            self.assertEqual(state["incidents"][-1]["event"], "quarantine-reconciled")

            # a policy-driven pause is NEVER auto-reconciled -- answering
            # again isn't proof the compliance issue itself is resolved
            state = {"quarantined": {"A": {"reason": "compliance-auto", "ts": "x"}}}
            self.assertFalse(_reconcile_quarantine(state, "A", up=True))
            self.assertFalse(_reconcile_quarantine(state, "A", up=True))
            self.assertIn("A", state["quarantined"])

            # nothing quarantined: no-op, no crash
            self.assertFalse(_reconcile_quarantine({}, "A", up=True))

        def test_passcode_attempt(self):
            # ADDED 2026-08-17: manual Quarantine/Resume previously required
            # no authentication at all on a dashboard the open internet can
            # reach. This exercises the pure decision logic directly, no
            # state file involved -- see check_quarantine_passcode() for the
            # file-backed wrapper.
            global _ACTION_PASSCODE
            import unittest.mock as mock
            now = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.timezone.utc)

            # correct passcode: allowed, nothing recorded as a failure
            state = {}
            allowed, reason = _passcode_attempt(state, "A", _ACTION_PASSCODE, now)
            self.assertTrue(allowed)
            self.assertEqual(reason, "")
            self.assertNotIn("A", state.get("auth_failures", {}))

            # wrong passcode: rejected, one failure recorded, one incident logged
            state = {}
            allowed, reason = _passcode_attempt(state, "A", "wrong", now)
            self.assertFalse(allowed)
            self.assertEqual(reason, "wrong passcode")
            self.assertEqual(len(state["auth_failures"]["A"]), 1)
            self.assertEqual(state["incidents"][-1]["event"], "quarantine-auth-failed")

            # already locked: rejected before the passcode is even checked --
            # a CORRECT passcode against a locked agent still fails.
            state = {"auth_locks": {"A": now.isoformat()}}
            allowed, reason = _passcode_attempt(state, "A", _ACTION_PASSCODE, now)
            self.assertFalse(allowed)
            self.assertIn("locked out", reason)

            # unconfigured passcode fails closed, even for what would
            # otherwise be the right value. Direct global reassignment, not
            # mock.patch("noc._ACTION_PASSCODE", ...) -- running this file as
            # `python noc.py` makes it __main__, and mock.patch("noc...")
            # would import a SEPARATE second copy of this module and patch
            # that one's global, leaving the one _passcode_attempt actually
            # reads untouched (httpx is fine to patch this way instead,
            # elsewhere, since it's a shared third-party module singleton
            # cached in sys.modules either way -- this plain string constant
            # is not).
            saved_passcode = _ACTION_PASSCODE
            _ACTION_PASSCODE = ""
            try:
                state = {}
                allowed, reason = _passcode_attempt(state, "A", "anything", now)
                self.assertFalse(allowed)
                self.assertIn("not configured", reason)
            finally:
                _ACTION_PASSCODE = saved_passcode

            # 5th consecutive failure locks the agent out and pages Telegram --
            # mock send_telegram so this never sends a real message.
            state = {"auth_failures": {"A": [now.timestamp()] * 4}}
            with mock.patch("noc.alerts.send_telegram", return_value=True) as sent:
                allowed, reason = _passcode_attempt(state, "A", "still-wrong", now)
            self.assertFalse(allowed)
            self.assertIn("A", state["auth_locks"])
            sent.assert_called_once()
            self.assertEqual(state["incidents"][-1]["event"], "auth-locked")

            # failures outside the window don't count toward the lockout
            old = now - dt.timedelta(seconds=_AUTH_FAIL_WINDOW_SEC + 60)
            state = {"auth_failures": {"A": [old.timestamp()] * 4}}
            with mock.patch("noc.alerts.send_telegram", return_value=True) as sent:
                allowed, reason = _passcode_attempt(state, "A", "still-wrong", now)
            self.assertNotIn("A", state.get("auth_locks", {}))
            sent.assert_not_called()

        def test_staleness_restart_gating(self):
            # enforced-cadence agent: stale readiness warrants a restart
            enforced = {"restart_on_staleness": True}
            # usage-driven agent (Study Platform): staleness never warrants one
            usage = {"restart_on_staleness": False}
            self.assertTrue(_restart_warranted(enforced, up=True, readiness="stale"))
            self.assertFalse(_restart_warranted(usage, up=True, readiness="stale"))
            # a liveness failure always warrants a restart, even for usage-driven
            self.assertTrue(_restart_warranted(usage, up=False, readiness="stale"))
            self.assertTrue(_restart_warranted(enforced, up=False, readiness="ok"))
            # healthy/skipped results never warrant a restart
            self.assertFalse(_restart_warranted(enforced, up=True, readiness="ok"))
            self.assertFalse(_restart_warranted(enforced, up=True, readiness="skipped"))
            self.assertFalse(_restart_warranted(enforced, up=True, readiness="n/a"))
            # staleness with an unstable dependency never warrants a restart
            self.assertFalse(_restart_warranted(enforced, up=True, readiness="stale",
                                                deps_stable=False))
            # liveness still warrants a restart even while deps flap
            self.assertTrue(_restart_warranted(enforced, up=False, readiness="stale",
                                               deps_stable=False))

        def test_flapping_dependency_replay(self):
            """Replay of the 2026-08-14 15:14-17:41 UTC window: chatanywhere
            flapped down/up/down/up (single healthy cycles between down
            episodes). With consecutive-cycle confirmation, neither
            confirmed_down nor confirmed_healthy ever becomes true -- so the
            three staleness-triggered restarts that locked Quant Paper that
            evening are all suppressed."""
            _DEPS_STREAK.clear()
            # the documented probe pattern: down, then a single up, repeated
            pattern = [False, True, False, True, False, True, False, True]
            suppressed = 0
            for ok in pattern:
                streak = _update_dep_streak("LLM API", ok)
                self.assertFalse(_dep_confirmed_healthy(streak),
                                 "single-cycle up must never confirm the dep stable")
                self.assertFalse(_dep_confirmed_down(streak),
                                 "single-cycle down must never confirm the dep down")
                deps_stable = all(_dep_confirmed_healthy(s)
                                  for s in _DEPS_STREAK.values())
                if not _restart_warranted({"restart_on_staleness": True},
                                          up=True, readiness="stale",
                                          deps_stable=deps_stable):
                    suppressed += 1
            self.assertEqual(suppressed, len(pattern),
                             "every staleness-restart opportunity in the flap window "
                             "must be suppressed")
            _DEPS_STREAK.clear()
            # two consecutive failures DO confirm the dependency down (blocked)
            _update_dep_streak("LLM API", False)
            self.assertTrue(_dep_confirmed_down(_update_dep_streak("LLM API", False)))
            # recovery is asymmetric on purpose: after a confirmed outage the
            # dependency must hold healthy across consecutive cycles again
            # (climbing back through zero) before staleness restarts resume --
            # one or two good readings off the back of a flap prove nothing.
            self.assertFalse(_dep_confirmed_healthy(_update_dep_streak("LLM API", True)))
            self.assertFalse(_dep_confirmed_healthy(_update_dep_streak("LLM API", True)))
            self.assertFalse(_dep_confirmed_healthy(_update_dep_streak("LLM API", True)))
            self.assertTrue(_dep_confirmed_healthy(_update_dep_streak("LLM API", True)))
            self.assertTrue(_restart_warranted({"restart_on_staleness": True},
                                               up=True, readiness="stale",
                                               deps_stable=True))
            _DEPS_STREAK.clear()

        def test_idle_counts_healthy_for_uptime(self):
            # usage-driven agent (Study Platform) stale = idle: the agent is
            # fine, just unused -- counts healthy. Keyed off enforced_cadence,
            # not restart_on_staleness (both happen to be False here too, but
            # enforced_cadence is the one _check_healthy actually reads).
            usage = {"restart_on_staleness": False, "enforced_cadence": False}
            enforced = {"restart_on_staleness": True, "enforced_cadence": True}
            self.assertTrue(_check_healthy(usage, up=True, readiness="stale"))
            self.assertFalse(_check_healthy(enforced, up=True, readiness="stale"))
            self.assertFalse(_check_healthy(usage, up=False, readiness="stale"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="ok"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="skipped"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="n/a"))

        def test_enforced_cadence_diverges_from_restart_on_staleness(self):
            # ADDED 2026-08-17: Quant Live's actual shape -- a real enforced
            # cadence (staleness IS a fault, counts against uptime) but never
            # auto-heal-eligible. Proves the two fields are read independently,
            # not that one derives from the other.
            quant_live = {"restart_on_staleness": False, "enforced_cadence": True}
            # display/uptime: staleness is a real fault (enforced_cadence=True)
            self.assertFalse(_check_healthy(quant_live, up=True, readiness="stale"))
            # restart authority: never warranted (restart_on_staleness=False),
            # even though enforced_cadence says this staleness is a real fault
            self.assertFalse(_restart_warranted(quant_live, up=True, readiness="stale"))
            # a liveness failure is unaffected by either field
            self.assertTrue(_restart_warranted(quant_live, up=False, readiness="ok"))

        def test_environment_scoped_freshness_lookup(self):
            # ADDED 2026-08-17: Paper and Live share project_tag "quant" --
            # environment_tag must additionally scope the Supabase query, or
            # monitoring both lets each one's freshness leak into the other's.
            paper = {"project_tag": "quant", "environment_tag": "paper"}
            live = {"project_tag": "quant", "environment_tag": "live"}
            neither = {"project_tag": "quant"}
            captured = {}

            class _FakeResp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return []

            def _fake_get(url, params, headers, timeout):
                captured["params"] = params
                return _FakeResp()

            import unittest.mock as mock
            with mock.patch("noc.httpx.get", side_effect=_fake_get):
                _latest_write(paper)
                self.assertEqual(captured["params"].get("environment"), "eq.paper")
                _latest_write(live)
                self.assertEqual(captured["params"].get("environment"), "eq.live")
                _latest_write(neither)
                self.assertNotIn("environment", captured["params"])

        def test_uptime(self):
            state = {}
            now = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)
            for i in range(10):
                _record_check(state, "X", healthy=(i % 2 == 0), now=now)
            self.assertEqual(_uptime_7d(state, "X", now), 50.0)
            self.assertIsNone(_uptime_7d(state, "Y", now))
            # checks older than the 7-day window don't count
            old = now - dt.timedelta(days=8)
            _record_check(state, "X", healthy=False, now=old)
            self.assertEqual(_uptime_7d(state, "X", now), 50.0)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NocSelftest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    _selftest()
