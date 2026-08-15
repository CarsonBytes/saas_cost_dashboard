"""Compliance Radar engine -- ADDED 2026-08-15.

Reads the `governance_rules` / `governance_audit_log` tables (created by the
user in the Supabase SQL editor -- see the round's Phase 0 SQL) over PostgREST
with the same service-role connection the ledger uses. No `agents` table
exists in this project, so rule conditions are matched against the registry
in services.py by construction -- the registry is the source of agent truth.

Two jobs, both deterministic by design (LLM is NEVER used to decide whether
to alert; at most it would parse raw text into the JSON this engine reads):

  1. Deadline enforcement: any PENDING rule whose enforcement_deadline has
     passed flips to OVERDUE, logs a STATUS_CHANGED audit row, and pushes a
     critical Telegram alert via alerts.py (never duplicate the Telegram
     logic).
  2. Rule matching: the current compliance snapshot -- a pluggable source,
     fetch_compliance_snapshot(), which currently returns {} because no real
     source is wired yet (RegTech Radar serves a UI + raw regulatory
     documents, no parsed-JSON endpoint) -- is matched against each active
     rule's condition_json in pure Python. Matching is therefore dormant
     until a source exists; the deadline path is fully live.

Everything is best-effort and never raises: a broken Supabase call or a
missing table must not kill the background loop (same resilience pattern as
noc.py::refresh_health).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time

import httpx

import alerts
import ledger  # SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / to_hkt

log = logging.getLogger(__name__)

_TABLES = ("governance_rules", "governance_audit_log")

# Rule ids that already alerted for a snapshot match -- (rule_id) -> epoch ts,
# cooldown one day, so a persistent match doesn't spam Telegram every cycle.
_MATCH_COOLDOWN_SEC = 86400
_MATCH_CACHE: dict[str, float] = {}

# UI snapshot cache. Page renders must NEVER do network I/O on the event loop
# (the app's rule of thumb -- it turns a slow Supabase into a frozen page);
# the background compliance loop fills this, and the Governance tab reads it.
# FIXED 2026-08-15: the tab initially fetched directly during render, which
# queued blocking calls on the loop and froze the app under rapid connections.
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"tables_ready": False, "rules": [], "audit": []}


def _headers() -> dict:
    return {"apikey": ledger.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {ledger.SUPABASE_SERVICE_ROLE_KEY}"}


def _get(table: str, params: dict) -> list[dict] | None:
    try:
        resp = httpx.get(f"{ledger.SUPABASE_URL}/rest/v1/{table}",
                         params=params, headers=_headers(), timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:                              # noqa: BLE001
        return None


def _post(table: str, payload: dict) -> bool:
    try:
        resp = httpx.post(f"{ledger.SUPABASE_URL}/rest/v1/{table}",
                          json=payload, headers={**_headers(), "Content-Type": "application/json",
                                                 "Prefer": "return=minimal"},
                          timeout=10)
        return resp.status_code < 300
    except Exception:                              # noqa: BLE001
        return False


def _patch(table: str, row_id: str, payload: dict) -> bool:
    try:
        resp = httpx.patch(f"{ledger.SUPABASE_URL}/rest/v1/{table}",
                           params={"id": f"eq.{row_id}"}, json=payload,
                           headers={**_headers(), "Content-Type": "application/json",
                                    "Prefer": "return=minimal"},
                           timeout=10)
        return resp.status_code < 300
    except Exception:                              # noqa: BLE001
        return False


def tables_ready() -> bool:
    """Both governance tables exist (the user ran the Phase 0 SQL)."""
    return all(_get(t, {"select": "id", "limit": "1"}) is not None for t in _TABLES)


# ---- UI snapshot cache (background-filled, render-safe) ---------------------

def refresh_cache() -> dict:
    """Best-effort snapshot for the Governance tab. Called from background
    tasks only (the compliance loop, mark_complied) -- never from a page
    render. Never raises."""
    try:
        with _CACHE_LOCK:
            _CACHE["tables_ready"] = tables_ready()
            if _CACHE["tables_ready"]:
                _CACHE["rules"] = fetch_rules()
                _CACHE["audit"] = get_audit_log()
            else:
                _CACHE["rules"], _CACHE["audit"] = [], []
        return dict(_CACHE)
    except Exception:                                 # noqa: BLE001
        log.exception("governance: cache refresh failed")
        return dict(_CACHE)


def cached_tables_ready() -> bool:
    with _CACHE_LOCK:
        return _CACHE["tables_ready"]


def cached_rules() -> list[dict]:
    with _CACHE_LOCK:
        return list(_CACHE["rules"])


def cached_audit() -> list[dict]:
    with _CACHE_LOCK:
        return list(_CACHE["audit"])


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        inst = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if inst.tzinfo is None:
            inst = inst.replace(tzinfo=dt.timezone.utc)
        return inst
    except ValueError:
        return None


def fetch_rules(statuses: tuple[str, ...] = ("PENDING", "OVERDUE")) -> list[dict]:
    """Active rules (default: PENDING + OVERDUE), soonest deadline first."""
    if statuses:
        return _get("governance_rules",
                    {"select": "*", "status": f"in.({','.join(statuses)})",
                     "order": "enforcement_deadline.asc"}) or []
    return _get("governance_rules", {"select": "*", "order": "enforcement_deadline.asc"}) or []


def get_audit_log(limit: int = 50) -> list[dict]:
    """Most recent audit rows first, with the rule name resolved (we resolve
    it in Python rather than embedding via PostgREST to avoid a 400 if the FK
    relationship isn't exposed)."""
    rows = _get("governance_audit_log",
                {"select": "*", "order": "created_at.desc", "limit": str(limit)}) or []
    if not rows:
        return rows
    rules = {r["id"]: r["rule_name"] for r in _get("governance_rules", {"select": "id,rule_name"}) or []}
    for row in rows:
        row["rule_name"] = rules.get(row.get("rule_id"), row.get("rule_id") or "—")
    return rows


def _audit(rule_id: str | None, action: str, actor: str, metadata: dict) -> bool:
    return _post("governance_audit_log", {
        "rule_id": rule_id, "action_taken": action, "actor": actor,
        "metadata": json.dumps(metadata) if metadata else None,
    })


def fetch_compliance_snapshot() -> dict:
    """Pluggable source for the current compliance state, e.g.
    {"impact_level": "high", "affected_domains": ["HR"]}.

    Returns {} today: no real source is wired yet (RegTech Radar serves a UI
    and raw regulatory documents, not a parsed-JSON endpoint), so matching is
    dormant. The deadline/OVERDUE path does not depend on this.
    """
    return {}


def _matches(rule: dict, snapshot: dict) -> bool:
    """Deterministic match of a rule's condition_json against the snapshot.

    condition_json is {"field": [allowed, values, ...], ...}; EVERY field must
    have a non-empty intersection with the snapshot's value for that field
    (snapshot values may be a single string or a list). Empty condition or
    empty snapshot never matches -- an unguarded vacuous match would alert on
    everything.
    """
    condition = rule.get("condition_json") or {}
    if not condition or not snapshot:
        return False
    for field, allowed in condition.items():
        allowed = allowed if isinstance(allowed, list) else [allowed]
        actual = snapshot.get(field)
        if actual is None:
            return False
        actual = actual if isinstance(actual, list) else [actual]
        if not set(map(str, allowed)) & set(map(str, actual)):
            return False
    return True


def _deadline_passed(rule: dict, now: dt.datetime) -> bool:
    deadline = _parse_ts(rule.get("enforcement_deadline"))
    return deadline is not None and now > deadline


def check_pending_rules() -> dict:
    """One compliance cycle (background Loop B, every 600s). Never raises."""
    try:
        return _check_pending_rules()
    except Exception:                              # noqa: BLE001
        log.exception("governance: compliance cycle failed")
        return {"ok": False}


def _check_pending_rules() -> dict:
    if not tables_ready():
        return {"ok": False, "tables": False}
    now = dt.datetime.now(dt.timezone.utc)
    rules = fetch_rules()
    flipped, matched = [], []
    snapshot = fetch_compliance_snapshot()
    for rule in rules:
        # 1. deadline enforcement: PENDING -> OVERDUE once, with audit + alert
        if rule.get("status") == "PENDING" and _deadline_passed(rule, now):
            if _patch("governance_rules", rule["id"], {"status": "OVERDUE"}):
                _audit(rule["id"], "STATUS_CHANGED", "system",
                       {"from": "PENDING", "to": "OVERDUE",
                        "deadline": rule.get("enforcement_deadline")})
                deadline_txt = ledger.to_hkt(rule["enforcement_deadline"]).strftime("%Y-%m-%d %H:%M")
                alerts.send_telegram(
                    f"compliance rule '{rule['rule_name']}' is now OVERDUE "
                    f"(deadline {deadline_txt} HKT) -- review required",
                    tag="GOV", emoji="\u26a0\ufe0f")
                flipped.append(rule["rule_name"])
        # 2. deterministic matching (dormant until a snapshot source exists)
        if snapshot and _matches(rule, snapshot) \
                and now.timestamp() - _MATCH_CACHE.get(rule["id"], 0) >= _MATCH_COOLDOWN_SEC:
            alerts.send_telegram(
                f"compliance rule '{rule['rule_name']}' matched current compliance state",
                tag="GOV", emoji="\u26a0\ufe0f")
            _audit(rule["id"], "AUTO_ALERT_SENT", "system",
                   {"snapshot": snapshot})
            _MATCH_CACHE[rule["id"]] = now.timestamp()
            matched.append(rule["rule_name"])
    refresh_cache()  # keep the UI snapshot fresh (runs in this background thread)
    return {"ok": True, "rules_checked": len(rules), "overdue": flipped, "matched": matched}


def mark_complied(rule_id: str, actor: str = "dashboard") -> bool:
    """UI action: mark a rule COMPLIED and record it in the audit trail."""
    ok = _patch("governance_rules", rule_id, {"status": "COMPLIED"})
    if ok:
        _audit(rule_id, "MANUAL_OVERRIDE", actor, {"to": "COMPLIED"})
    refresh_cache()  # so the tab reflects the change on its next render
    return ok


# ---- selftest (stdlib only) -------------------------------------------------

def _selftest() -> None:
    import unittest

    class GovernanceSelftest(unittest.TestCase):
        def test_matcher(self):
            rule = {"condition_json": {"impact_level": ["high"]}}
            self.assertTrue(_matches(rule, {"impact_level": "high"}))
            self.assertTrue(_matches(rule, {"impact_level": ["high"]}))
            self.assertFalse(_matches(rule, {"impact_level": "low"}))
            self.assertFalse(_matches(rule, {"other": "x"}))
            # multi-field is AND: every field must intersect
            both = {"condition_json": {"impact_level": ["high"],
                                       "affected_domains": ["HR", "Finance"]}}
            self.assertTrue(_matches(both, {"impact_level": "high", "affected_domains": ["HR"]}))
            self.assertFalse(_matches(both, {"impact_level": "high", "affected_domains": ["Legal"]}))
            # empty condition / empty snapshot never matches (no vacuous alert)
            self.assertFalse(_matches({"condition_json": {}}, {"impact_level": "high"}))
            self.assertFalse(_matches(rule, {}))
            self.assertFalse(_matches({}, {"impact_level": "high"}))

        def test_deadline(self):
            now = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
            past = {"enforcement_deadline": "2026-08-14T00:00:00+00:00"}
            future = {"enforcement_deadline": "2026-08-20T00:00:00+00:00"}
            no_deadline = {"enforcement_deadline": None}
            self.assertTrue(_deadline_passed(past, now))
            self.assertFalse(_deadline_passed(future, now))
            self.assertFalse(_deadline_passed(no_deadline, now))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GovernanceSelftest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    _selftest()
