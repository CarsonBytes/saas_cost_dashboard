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


def send_telegram(message: str, tag: str = "LLM-COST", emoji: str = "\U0001f4b8") -> bool:
    """Push `message` to the configured Telegram chat. Defaults preserve the
    original cost-alert shape (💸 [LLM-COST] ...); the NOC layer (noc.py)
    passes tag="NOC" and a different emoji so its alerts read distinctly in
    the same chat."""
    if not is_telegram_configured():
        log.debug("alerts: TELEGRAM_BOT_TOKEN/CHAT_ID not set, skipping: %s", message)
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{emoji} [{tag}] {message}"},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("alerts: Telegram API returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001 -- alerting must never raise
        log.warning("alerts: failed to send Telegram alert: %s", e)
        return False


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
            f"today's LLM spend is ${cost_today:.4f}, over the ${ALERT_DAILY_COST_USD:.2f} threshold"
        )
    return result
