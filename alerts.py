"""Daily cost-threshold alerting: dashboard banner (in-process state) +
Telegram push. Telegram send mirrors D:\\quant\\dashboard\\core\\notify.py's
convention exactly (same bot API call shape, best-effort/non-raising, emoji +
tag prefix) so a cost alert reads consistently next to quant's own [PAPER]/
[LIVE] alerts in the same chat -- reuses the same TELEGRAM_BOT_TOKEN/
TELEGRAM_CHAT_ID credentials (see D:\\quant\\analyst\\.env).

Same-day dedup is file-backed (not in-memory) because this needs to survive
both a dashboard restart and being called from a separate headless process
(e.g. a scheduled task) -- an in-memory cooldown wouldn't be shared between
those.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

import ledger  # for the shared HKT-day helper -- same "today" as the dashboard's own charts

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

# ADDED 2026-08-18: lives under state/, a named Docker volume (see
# docker-compose.yml) shared with noc.py's state file -- previously this sat
# in the container's writable layer with nothing mounted, so every redeploy
# silently reset the alert-dedup history and any dashboard-set threshold back
# to the .env default. mkdir here so local (non-Docker) runs and the very
# first container start both just work, before _load_threshold() below reads it.
_STATE_DIR = Path(__file__).parent / "state"
_STATE_DIR.mkdir(parents=True, exist_ok=True)
_SETTINGS_FILE = _STATE_DIR / "alert_settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_settings(settings: dict) -> None:
    _SETTINGS_FILE.write_text(json.dumps(settings))


def _load_threshold() -> float:
    """A dashboard-set threshold (persisted so it survives a restart of the
    always-on process) overrides the .env default -- editing .env would
    otherwise require touching the server and restarting it for something
    that's meant to be adjustable from the UI."""
    try:
        return float(json.loads(_SETTINGS_FILE.read_text())["alert_daily_cost_usd"])
    except (FileNotFoundError, ValueError, KeyError):
        return float(os.environ.get("ALERT_DAILY_COST_USD", "0.50"))


ALERT_DAILY_COST_USD = _load_threshold()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_STATE_FILE = _STATE_DIR / "alert_state.json"
_HISTORY_LIMIT = 50


def set_daily_threshold(value: float) -> None:
    global ALERT_DAILY_COST_USD
    ALERT_DAILY_COST_USD = value
    # Read-modify-write so a second dashboard-set setting (monthly budget)
    # sharing this file survives (FIXED 2026-08-26: the original whole-file
    # overwrite would have silently erased it).
    settings = _load_settings()
    settings["alert_daily_cost_usd"] = value
    _save_settings(settings)


# ---- configurable monthly budget --------------------------------------------
# The projection KPI previously compared against an implied budget of
# threshold x 30 -- a proxy, never independently set. Now a first-class,
# separately-persisted setting; unset falls back to the old implied figure.

def _load_budget() -> float | None:
    value = _load_settings().get("monthly_budget_usd")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


MONTHLY_BUDGET_USD = _load_budget()


def set_monthly_budget(value: float | None) -> None:
    """None clears the override -- the KPI falls back to threshold x 30."""
    global MONTHLY_BUDGET_USD
    MONTHLY_BUDGET_USD = value
    settings = _load_settings()
    if value is None:
        settings.pop("monthly_budget_usd", None)
    else:
        settings["monthly_budget_usd"] = value
    _save_settings(settings)


def effective_monthly_budget() -> float:
    return MONTHLY_BUDGET_USD if MONTHLY_BUDGET_USD else ALERT_DAILY_COST_USD * 30


def _today() -> str:
    """HKT calendar date, not UTC -- the shared chatanywhere.tech key's own
    daily quota resets on HKT's day boundary (see quant/event_radar's own
    fetch_shared_usage_today() fixes), and this dashboard's alert threshold
    is a "today's cost" check, so it needs to agree with the same "today"
    everything else in this ecosystem already uses."""
    return dt.datetime.now(ledger._HKT).strftime("%Y-%m-%d")


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state))


def get_history(limit: int = 20) -> list[dict]:
    """Most recent fired alerts first -- for the dashboard's alert-history
    table. Separate from the dedup state itself so a restart never loses
    the record of what already fired."""
    history = _load_state().get("history", [])
    return list(reversed(history))[:limit]


def is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


# ---- severity routing (docs/NOTIFICATION_SPEC.md) ---------------------------
# Telegram is a PAGING channel, not a log sink: only `critical` reaches it.
# Everything else lands in the log and rolls up into one daily digest. The
# decision rule from the spec: "does ignoring this for 12 hours make it
# worse?" -- if not, it isn't critical.
LEVELS = ("critical", "error", "warning", "info")
PUSH_LEVELS = frozenset({"critical"})

# Anti-flood guarantees (spec 5): a hard daily cap plus two cooldowns. The cap
# is what makes "Telegram is too noisy" structurally impossible to regress --
# no single bug can page more than CRITICAL_DAILY_CAP times in a day.
CRITICAL_DAILY_CAP = int(os.environ.get("ALERT_CRITICAL_DAILY_CAP", "10"))
COOLDOWN_KEY_SEC = int(os.environ.get("ALERT_COOLDOWN_KEY_SEC", "900"))
COOLDOWN_TEXT_SEC = int(os.environ.get("ALERT_COOLDOWN_TEXT_SEC", "300"))

# The digest is deliberately opt-in for Telegram (spec 3): the operator asked
# for critical-only pushes, so the default channel for error/warning rollup is
# the dashboard. Set DIGEST_TELEGRAM=1 to also receive it at 08:00 HKT.
DIGEST_TELEGRAM = os.environ.get("DIGEST_TELEGRAM", "0").strip() in ("1", "true", "yes")
DIGEST_HOUR_HKT = 8
DIGEST_ITEMS = 5

# Spec 6: every attempt -- sent, suppressed, failed -- is appended here.
# Nothing about alert delivery may live only in the sender's memory: an alert
# that was sent and an alert that was dropped must be distinguishable after
# the fact. Append-only JSONL, one object per line.
#
# ALERT_AUDIT_DIR points at a bind mount (../claude/alerts/audit -> /app/
# alerts_audit, see docker-compose.yml) so the HOST-side infra-watchdog appends
# to the same file this process reads: one trail covering every sender, which
# is the only way the digest can report what did NOT page. Falls back to
# state/ for local runs and tests, where no mount exists.
_AUDIT_DIR = Path(os.environ.get("ALERT_AUDIT_DIR") or _STATE_DIR)
try:
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    log.debug("alerts: audit dir %s not creatable yet", _AUDIT_DIR, exc_info=True)
_AUDIT_FILE = _AUDIT_DIR / "alerts.jsonl"
_AUDIT_LIMIT_LINES = 5000


def _audit(record: dict) -> None:
    """Append one delivery attempt to state/alerts.jsonl. Never raises -- a
    broken audit trail must not be able to break the alert it is describing."""
    try:
        with _AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Bounded: rotate in place once it grows past the limit, so a year of
        # suppressed-but-noted attempts can't fill the volume.
        if _AUDIT_FILE.stat().st_size > 4 * 1024 * 1024:
            lines = _AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            _AUDIT_FILE.write_text("\n".join(lines[-_AUDIT_LIMIT_LINES:]) + "\n",
                                   encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("alerts: audit append failed", exc_info=True)


def _bucket() -> dict:
    """Push bookkeeping (per-key/per-text cooldowns, daily critical counter,
    digest-sent marker). One read-modify-write per call site -- alerts fire
    rarely, so a file read is free compared to the ambiguity of in-memory
    state that a restart would erase.

    The digest itself is NOT stored here: it is derived from _AUDIT_FILE so
    every producer -- including the bash watchdog writing the same file --
    feeds one rollup with no cross-process state to keep in sync."""
    state = _load_state()
    today = _today()
    if state.get("cap_date") != today:
        state["cap_date"] = today
        state["cap_count"] = 0
        state["cap_collapsed"] = False
    state.setdefault("push_key_ts", {})
    state.setdefault("push_text_ts", {})
    return state


def _prune_cooldowns(state: dict) -> None:
    """Drop cooldown entries older than the longest window -- otherwise these
    two dicts grow by one entry per distinct alert key for the lifetime of the
    state file."""
    cutoff = time.time() - max(COOLDOWN_KEY_SEC, COOLDOWN_TEXT_SEC) - 60
    for field in ("push_key_ts", "push_text_ts"):
        bucket = state.get(field, {})
        for k in [k for k, iso in bucket.items()
                  if iso and dt.datetime.fromisoformat(iso).timestamp() < cutoff]:
            del bucket[k]


def _digest_entries() -> list[dict]:
    """Today's non-pushed `error`/`warning` attempts, oldest first.

    Reading the audit trail (spec 6) instead of a parallel counter is what
    makes the digest a true accounting of what did NOT page -- which is the
    only thing that makes a quiet Telegram channel trustworthy rather than
    merely silent. Any producer that appends to alerts.jsonl participates."""
    today = _today()
    out: list[dict] = []
    try:
        for line in _AUDIT_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not rec.get("digest_eligible"):
                continue
            if rec.get("level") not in ("error", "warning"):
                continue
            if _hkt_date(str(rec.get("ts", ""))) != today:
                continue
            out.append(rec)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        log.debug("alerts: digest read failed", exc_info=True)
    return out


def _hkt_date(iso_ts: str) -> str:
    """HKT calendar date for an ISO-8601 timestamp, tolerant of a missing or
    malformed value (returns '' so callers can compare and drop the row)."""
    try:
        stamp = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(ledger._HKT).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _cooldown_reason(state: dict, key: str, text: str) -> str | None:
    """'key' | 'text' | None -- whether this exact alert already went out
    within its cooldown window."""
    now = dt.datetime.now(dt.timezone.utc)
    if key:
        iso = state.get("push_key_ts", {}).get(key)
        if iso and (now - dt.datetime.fromisoformat(iso)).total_seconds() < COOLDOWN_KEY_SEC:
            return "key"
    iso = state.get("push_text_ts", {}).get(text)
    if iso and (now - dt.datetime.fromisoformat(iso)).total_seconds() < COOLDOWN_TEXT_SEC:
        return "text"
    return None


def send_telegram(message: str, tag: str = "LLM-COST", emoji: str = "\U0001f4b8",
                  level: str = "warning", key: str = "", force: bool = False) -> bool:
    """Send `message` at `level` through the routing policy in
    docs/NOTIFICATION_SPEC.md.

    Only `critical` actually reaches Telegram; every other level is recorded
    in the digest and returns False (so callers that treat the return value as
    "the operator was told" stay correct -- they weren't, by design).

    `key` groups an alert into one incident for cooldown purposes. Retry loops
    (the gateway's relogin cycles, a watchdog's restart attempts) must pass a
    stable key or they re-page on every attempt -- that single omission is
    what produced 2,488 pushes on 2026-09-26.

    `force=True` bypasses the severity filter ONLY. It exists for direct
    replies to an operator-initiated action (commands.py): a response the
    operator is waiting on is not an alert and cannot be dropped as
    "not critical enough". Everything else -- cooldown, cap, audit -- still
    applies.

    Defaults preserve the original cost-alert shape (💸 [LLM-COST] ...); the
    NOC layer passes tag="NOC" and a different emoji so its alerts read
    distinctly in the same chat."""
    if level not in LEVELS:
        level = "warning"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    digest_eligible = level in ("error", "warning")
    if level not in PUSH_LEVELS and not force:
        _audit({"ts": now_iso, "level": level, "source": tag, "key": key,
                "outcome": "suppressed-level", "http_status": None,
                "digest_eligible": digest_eligible, "text": message})
        log.debug("alerts: %s suppressed by level policy: %s", level, message)
        return False
    if not is_telegram_configured():
        _audit({"ts": now_iso, "level": level, "source": tag, "key": key,
                "outcome": "not-configured", "http_status": None,
                "digest_eligible": False, "text": message})
        log.debug("alerts: TELEGRAM_BOT_TOKEN/CHAT_ID not set, skipping: %s", message)
        return False

    state = _bucket()
    _prune_cooldowns(state)
    reason = _cooldown_reason(state, key, message)
    if reason:
        _save_state(state)
        _audit({"ts": now_iso, "level": level, "source": tag, "key": key,
                "outcome": f"suppressed-cooldown-{reason}", "http_status": None,
                "digest_eligible": False, "text": message})
        return False
    if state.get("cap_count", 0) >= CRITICAL_DAILY_CAP:
        # Collapse the remainder into ONE message per day rather than going
        # silent: the operator still learns the flood continued, exactly once.
        if not state.get("cap_collapsed"):
            state["cap_collapsed"] = True
            _save_state(state)
            return _post(f"+{CRITICAL_DAILY_CAP} more criticals suppressed "
                         f"today (cap reached) -- see the dashboard",
                         tag, "ℹ️", state, key, now_iso, level="critical",
                         collapsed=True)
        _save_state(state)
        _audit({"ts": now_iso, "level": level, "source": tag, "key": key,
                "outcome": "suppressed-cap", "http_status": None,
                "digest_eligible": False, "text": message})
        return False

    state["cap_count"] = state.get("cap_count", 0) + 1
    state.setdefault("push_key_ts", {})[key or message] = now_iso
    if message:
        state.setdefault("push_text_ts", {})[message] = now_iso
    _save_state(state)
    return _post(message, tag, emoji, state, key, now_iso, level=level)


def _post(message: str, tag: str, emoji: str, state: dict, key: str,
          now_iso: str, level: str = "critical", collapsed: bool = False) -> bool:
    """One HTTP attempt + its audit record. Split out so the cap-collapse
    message goes through the identical accounting path as a normal push."""
    http_status = None
    ok = False
    err = None
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{emoji} [{tag}] {message}"},
            timeout=10,
        )
        http_status = resp.status_code
        ok = resp.status_code == 200
        if not ok:
            log.warning("alerts: Telegram API returned %s: %s",
                        resp.status_code, resp.text[:200])
    except Exception as e:  # noqa: BLE001 -- alerting must never raise
        err = str(e)
        log.warning("alerts: failed to send Telegram alert: %s", e)
    _audit({"ts": now_iso, "level": level, "source": tag, "key": key,
            "outcome": "sent" if ok else "failed", "http_status": http_status,
            "digest_eligible": False, "collapsed": collapsed,
            "error": err, "text": message})
    return ok


def build_digest() -> str | None:
    """Today's error/warning rollup as a multi-line string, or None if nothing
    non-critical happened today. Pure read of the audit trail, so the dashboard
    can render it without triggering anything.

    Deliberately enumerates what did NOT page: a digest that only summarises
    successes would hide exactly the class of event the operator demoted."""
    entries = _digest_entries()
    if not entries:
        return None
    counts: dict[str, int] = {}
    for rec in entries:
        counts[rec["level"]] = counts.get(rec["level"], 0) + 1
    parts = [f"{n}\u00d7{lvl}" for lvl, n in sorted(counts.items(), reverse=True)]
    lines = [f"since 00:00 HKT: {', '.join(parts)} (not paged -- critical only)"]
    for rec in entries[-DIGEST_ITEMS:]:
        src = rec.get("source", "?")
        key = rec.get("key") or "unattributed"
        lines.append(f"  [{rec['level']}] {src}/{key}: {rec.get('text', '')}")
    return "\n".join(lines)


def maybe_send_digest() -> bool:
    """Fire the digest at DIGEST_HOUR_HKT once per HKT day (spec 3).

    Called from run_check, which already runs on every stats refresh -- no new
    timer, no new process. DIGEST_TELEGRAM gates the push: by default the
    digest exists only for the dashboard, because the operator asked for
    critical-only Telegram."""
    if not DIGEST_TELEGRAM:
        return False
    now = dt.datetime.now(ledger._HKT)
    if now.hour < DIGEST_HOUR_HKT:
        return False
    state = _bucket()
    if state.get("digest_sent_date") == _today():
        return False
    body = build_digest()
    if not body:
        return False
    state["digest_sent_date"] = _today()
    _save_state(state)
    return send_telegram(f"daily digest -- {body}", tag="DIGEST", emoji="\U0001f4ca",
                         level="critical", key=f"digest:{_today()}")


def check_daily_threshold(cost_today: float) -> dict:
    """Evaluate cost_today against ALERT_DAILY_COST_USD. Fires (and records)
    at most once per UTC day, but re-fires if spend has since grown another
    50% past the last-alerted amount, so a runaway day isn't reported once
    and then ignored.

    Returns {"breached": bool, "should_notify": bool, "threshold": float,
    "cost_today": float} -- "breached" drives the dashboard banner every time
    (even on repeat checks the same day); "should_notify" gates the Telegram
    push (only on new/worse breaches).
    """
    breached = cost_today > ALERT_DAILY_COST_USD
    result = {"breached": breached, "should_notify": False,
               "threshold": ALERT_DAILY_COST_USD, "cost_today": cost_today}
    if not breached:
        return result

    state = _load_state()
    today = _today()
    last_alerted_cost = state.get("last_alerted_cost", 0.0) if state.get("date") == today else 0.0
    if cost_today > last_alerted_cost * 1.5 or last_alerted_cost == 0.0:
        result["should_notify"] = True
        history = state.get("history", [])
        history.append({"date": today, "cost_today": cost_today, "threshold": ALERT_DAILY_COST_USD,
                         "fired_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        state.update({"date": today, "last_alerted_cost": cost_today,
                      "history": history[-_HISTORY_LIMIT:]})
        _save_state(state)
    return result


def run_check(cost_today: float) -> dict:
    """Evaluate the threshold and push a Telegram notification if warranted.
    Always safe to call repeatedly (e.g. from a ui.timer) -- notification
    itself is deduped by check_daily_threshold's state file."""
    result = check_daily_threshold(cost_today)
    if result["should_notify"]:
        send_telegram(
            f"today's LLM spend is ${cost_today:.4f}, over the ${ALERT_DAILY_COST_USD:.2f} threshold",
            level="warning", key="cost:daily",
        )
    try:
        maybe_send_digest()
    except Exception:  # noqa: BLE001 -- a digest must never break a stats refresh
        log.exception("daily digest check failed")
    return result
