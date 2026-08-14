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


def _dependencies_down() -> list[str]:
    """Independent confirmation of the shared upstream dependencies, from this
    dashboard's own vantage point (not from the agent under test)."""
    down = []
    if not _probe(f"{ledger.SUPABASE_URL.rstrip('/')}/rest/v1/"):
        down.append("Supabase")
    if not _probe(_LLM_API_BASE_URL):
        down.append("LLM API")
    return down


def _latest_write(tag: str) -> str | None:
    """Most recent created_at (ISO 8601, UTC) for a project tag in the shared
    llm_calls ledger, or None if the tag has no rows. Raises on Supabase
    failure -- callers treat that as stale + let the dependency probe decide
    whether it's a blocked-by situation."""
    resp = httpx.get(
        f"{ledger.SUPABASE_URL}/rest/v1/llm_calls",
        params={"select": "created_at", "project": f"eq.{tag}",
                "order": "created_at.desc", "limit": "1"},
        headers={"apikey": ledger.SUPABASE_SERVICE_ROLE_KEY,
                 "Authorization": f"Bearer {ledger.SUPABASE_SERVICE_ROLE_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["created_at"] if rows else None


def _latest_write_safe(tag: str) -> str | None:
    try:
        return _latest_write(tag)
    except Exception:                             # noqa: BLE001
        return None


# ---- readiness -------------------------------------------------------------

def _readiness(svc: dict, now: dt.datetime, last_restart: float | None,
               last_write_ts: str | None) -> tuple[str, str]:
    """Readiness state for a monitored agent. Returns (state, detail) where
    state is "ok" | "stale" | "skipped" | "n/a". Pure -- testable without
    network; refresh_health() fetches last_write_ts first."""
    if not svc.get("project_tag"):
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
    """Restart the agent's own Docker container via the Engine API over the
    mounted /var/run/docker.sock (same WSL2 daemon all projects share) --
    httpx's UDS transport avoids needing the docker CLI or the docker SDK."""
    try:
        transport = httpx.HTTPTransport(uds="/var/run/docker.sock")
        with httpx.Client(transport=transport, timeout=60) as client:
            resp = client.post(f"http://localhost/containers/{name}/restart")
        return resp.status_code < 300
    except Exception as e:                        # noqa: BLE001
        log.warning("noc: docker restart of %s failed: %s", name, e)
        return False


def _restart_count(state: dict, name: str, now: dt.datetime) -> int:
    window = now.timestamp() - _RESTART_WINDOW_SEC
    return sum(1 for ts in state.get("restarts", {}).get(name, []) if ts >= window)


def _restart_eligible(svc: dict, now: dt.datetime, state: dict) -> bool:
    """Permission gates for auto-restart (unhealthy/blocked gating lives in
    refresh_health, which has the fresh check results)."""
    if svc["restart"] != "auto_heal" or not svc.get("container"):
        return False
    if svc["name"] in state.get("locks", {}):
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
    ok = alerts.send_telegram(
        f"{name} locked: {_RESTART_LOCK_COUNT} restarts within the last hour "
        f"-- clear the lock from the dashboard",
        tag="NOC", emoji="\U0001f6a8")
    _add_incident(state, name, "alert sent",
                  outcome="telegram" if ok else "telegram failed", detail="lock alert")


def clear_lock(name: str) -> None:
    """UI action: unlock an auto-heal agent and reset its restart window so the
    cooldown doesn't immediately re-lock it. Logged to the incident log."""
    with _STATE_LOCK:
        state = _load_state()
        if name in state.get("locks", {}):
            del state["locks"][name]
            state.get("restarts", {}).pop(name, None)
            _add_incident(state, name, "unlocked", outcome="restart window reset")
        _save_state(state)
    if name in _STATUS_CACHE:
        _STATUS_CACHE[name]["locked"] = False


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

        deps_down = _dependencies_down()

        writers = [s for s in monitored if s.get("project_tag")]
        with ThreadPoolExecutor(max_workers=max(len(writers), 1)) as pool:
            last_write_map = dict(zip(
                [s["name"] for s in writers],
                pool.map(lambda s: _latest_write_safe(s["project_tag"]), writers)))

        prev = {name: dict(_STATUS_CACHE.get(name, {})) for name in up_map}

        for svc in monitored:
            name = svc["name"]
            up = up_map[name]
            locked = name in state.get("locks", {})
            restarts = state.get("restarts", {}).get(name, [])
            last_restart = restarts[-1] if restarts else None

            readiness, detail = "n/a", ""
            if svc.get("project_tag"):
                readiness, detail = _readiness(svc, now, last_restart,
                                               last_write_map.get(name))

            unhealthy = (not up) or (readiness == "stale")
            blocked_by = list(deps_down) if (unhealthy and deps_down) else []
            was_blocked = bool(prev.get(name, {}).get("blocked_by"))
            if blocked_by and not was_blocked:
                _add_incident(state, name, "blocked-by-dependency",
                              outcome=", ".join(blocked_by),
                              detail="restart suppressed while dependency is down")

            if unhealthy and svc["restart"] == "auto_heal":
                if blocked_by:
                    pass  # circuit breaker: dependency down, restarting can't help
                elif locked:
                    pass
                elif _restart_eligible(svc, now, state):
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

            # uptime: "healthy" = liveness ok AND readiness not stale (skipped
            # during grace / outside market hours counts as healthy)
            healthy = up and (readiness in ("ok", "skipped", "n/a"))
            _record_check(state, name, healthy, now)

            _STATUS_CACHE[name] = {
                "up": up,
                "readiness": readiness,
                "readiness_detail": detail,
                "blocked_by": blocked_by,
                "locked": locked,
                "last_write": last_write_map.get(name),
                "checked_at": time.time(),
                "uptime_7d": _uptime_7d(state, name, now),
                "alerted_unhealthy": (prev.get(name, {}).get("alerted_unhealthy", False)
                                       or unhealthy) if svc["restart"] == "alert_only" else False,
            }

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
