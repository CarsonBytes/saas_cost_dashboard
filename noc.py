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

Threading: everything here is blocking (HTTP probes, Supabase reads, Docker
Engine API calls, file writes). Call refresh_health() via asyncio.to_thread
from async code, never from a page-render path -- restarts fire only from the
background loop.

Readiness exceptions (both needed to avoid false alarms):
  1. 5-minute grace after a dashboard-initiated restart (still warming up).
  2. Quant Trading (Paper): skipped outside the real NYSE session -- computed
     in US Eastern time with DST via zoneinfo("America/New_York"), plus real
     US market holidays via pandas_market_calendars (the same approach quant's
     own dashboard/app.py::_market_open() + core/market_calendar.py use --
     that exact DST bug was found and fixed there first, so this mirrors it
     rather than reinventing a fixed-HK-range that drifts an hour wrong twice
     a year).

State: file-backed JSON (noc_state.json), same pattern as alerts.py's
alert_state.json -- incidents/restart counts/locks survive a dashboard
restart, and the state file is excluded from rsync/docker so the container's
copy is the only writer.
"""
from __future__ import annotations

import datetime as dt
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

_STATE_FILE = Path(__file__).parent / "noc_state.json"
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
    return all(_probe(url) for _, url in svc["links"])


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
    user answered several questions). Raises on Supabase failure -- callers
    treat that as stale + let the dependency probe decide whether it's a
    blocked-by situation."""
    table = svc.get("freshness_table", "llm_calls")
    params = {"select": "created_at", "order": "created_at.desc", "limit": "1"}
    tag = svc.get("project_tag")
    if table == "llm_calls" and tag:
        params["project"] = f"eq.{tag}"
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
    if svc["name"] == QUANT_PAPER and not nyse_session_open(now):
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
    Idle (stale but not restart_on_staleness -- e.g. Study Platform simply
    having no users) counts healthy, the same way a skipped check (grace
    period, market closed) already does: the agent itself is fine either way."""
    if not up:
        return False
    if readiness in ("ok", "skipped", "n/a"):
        return True
    return readiness == "stale" and not svc.get("restart_on_staleness")


def _restart_count(state: dict, name: str, now: dt.datetime) -> int:
    window = now.timestamp() - _RESTART_WINDOW_SEC
    return sum(1 for ts in state.get("restarts", {}).get(name, []) if ts >= window)


def _restart_eligible(svc: dict, now: dt.datetime, state: dict,
                      compliance: dict[str, list[str]] | None = None) -> bool:
    """Permission gates for auto-restart (unhealthy/blocked gating lives in
    refresh_health, which has the fresh check results). Quarantined agents --
    manually paused, or compliance-critical (an OVERDUE rule naming them) --
    are never auto-restarted."""
    if svc["restart"] != "auto_heal" or not svc.get("container"):
        return False
    if svc["name"] in state.get("locks", {}):
        return False
    if _quarantine_reason(svc["name"], state, compliance or {}):
        return False
    if svc["name"] == QUANT_PAPER and not nyse_session_open(now):
        return False
    restarts = state.get("restarts", {}).get(svc["name"], [])
    if restarts and now.timestamp() - restarts[-1] < _RESTART_GRACE_SEC:
        return False
    return True


def _lock_agent(state: dict, name: str, now: dt.datetime) -> None:
    state.setdefault("locks", {})[name] = now.isoformat()
    _add_incident(state, name, "locked", outcome="auto-heal disabled")
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


def _send_lock_alert(name: str) -> bool:
    """Send the lock Telegram alert, retrying once after a short pause -- the
    2026-08-14 failure was a transient network blip (the next alert eight
    seconds later landed), so one immediate retry would have caught it."""
    msg = (f"{name} locked: {_RESTART_LOCK_COUNT} restarts within the last hour "
           f"-- clear the lock from the dashboard")
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
    cooldown doesn't immediately re-lock it. Logged to the incident log."""
    with _STATE_LOCK:
        state = _load_state()
        if name in state.get("locks", {}):
            del state["locks"][name]
            state.get("restarts", {}).pop(name, None)
            _add_incident(state, name, "unlocked", outcome="restart window reset")
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

        for svc in monitored:
            name = svc["name"]
            up = up_map[name]
            locked = name in state.get("locks", {})
            restarts = state.get("restarts", {}).get(name, [])
            last_restart = restarts[-1] if restarts else None
            quarantine = _quarantine_reason(name, state, compliance)

            readiness, detail = "n/a", ""
            if svc.get("project_tag") or svc.get("freshness_table"):
                readiness, detail = _readiness(svc, now, last_restart,
                                               last_write_map.get(name))

            unhealthy = (not up) or (readiness == "stale")
            blocked_by = list(confirmed_down) if (unhealthy and confirmed_down) else []
            was_blocked = bool(prev.get(name, {}).get("blocked_by"))
            if blocked_by and not was_blocked:
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
            # Guards: only quarantinable agents with a container; never Quant
            # Paper during market hours (a pause mid-session is the operator's
            # call, not a rule's).
            auto_target = auto_targets.get(name)
            if (name not in state.get("quarantined", {}) and auto_target
                    and svc.get("quarantinable") and svc.get("container")):
                if name == QUANT_PAPER and nyse_session_open(now):
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

            if _restart_warranted(svc, up, readiness, deps_stable) \
                    and svc["restart"] == "auto_heal":
                if blocked_by:
                    pass  # circuit breaker: dependency confirmed down
                elif locked:
                    pass
                elif quarantine:
                    pass  # quarantined: never bounce while red/manually paused
                elif _restart_eligible(svc, now, state, compliance):
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
                ok = alerts.send_telegram(
                    f"{name} is down (liveness check failed)", tag="NOC",
                    emoji="\U0001f6a8")
                _add_incident(state, name, "alert sent",
                              outcome="telegram" if ok else "telegram failed",
                              detail="down alert")

            # uptime: "healthy" = liveness ok AND readiness not a real fault.
            # Idle (stale but not restart_on_staleness, e.g. Study Platform
            # simply having no users) counts healthy, the same way a skipped
            # check (grace / market closed) already does.
            healthy = _check_healthy(svc, up, readiness)
            _record_check(state, name, healthy, now)

            _STATUS_CACHE[name] = {
                "up": up,
                "readiness": readiness,
                "readiness_detail": detail,
                "blocked_by": blocked_by,
                "locked": locked,
                "quarantined": quarantine,  # None | 'manual' | 'compliance'
                "last_write": last_write_map.get(name),
                "checked_at": time.time(),
                "uptime_7d": _uptime_7d(state, name, now),
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
            # quant paper outside market hours -> skipped (Sat)
            qp = {**svc, "name": QUANT_PAPER}
            sat = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
            self.assertEqual(_readiness(qp, sat, None, stale)[0], "skipped")
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
            # fine, just unused -- counts healthy, never restart-warranted.
            usage = {"restart_on_staleness": False}
            enforced = {"restart_on_staleness": True}
            self.assertTrue(_check_healthy(usage, up=True, readiness="stale"))
            self.assertFalse(_check_healthy(enforced, up=True, readiness="stale"))
            self.assertFalse(_check_healthy(usage, up=False, readiness="stale"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="ok"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="skipped"))
            self.assertTrue(_check_healthy(usage, up=True, readiness="n/a"))

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
