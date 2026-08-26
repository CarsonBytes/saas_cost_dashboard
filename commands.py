"""Telegram control plane (Phase 5 A2) -- the NOC's alerts already land in
the operator's chat; this turns replies into actions so a 2am lock never
requires opening the dashboard. Human-informed stays the principle: every
command is executed, logged to the incident log and echoed back.

Commands (matched case-insensitively, agent names by substring):
    /unlock <agent>   clear an auto-heal lock (operator override -- also
                      resets strike escalation, same as the dashboard button)
    /status           one-glance health: per-agent dot-state + today's cost
    /mute <minutes> [agent-substring ...]
                      arm a maintenance window (deploy bouncing containers
                      must not trip restarts/alerts/uptime); no agent list =
                      global. /unmute clears it.

Security: only messages from the configured TELEGRAM_CHAT_ID are acted on --
anything else is ignored without reply (this is not a public interface).

Polling is OPT-IN via a DEDICATED bot token (TELEGRAM_COMMAND_BOT_TOKEN):
getUpdates long-poll conflicts with any other consumer of the same bot
(409 -- the alert bot may be shared with quant; and our own dev instance vs
the deployed container would fight each other, found live 2026-08-26).
Unset = the control plane stays off and the dashboard buttons remain the
way to act.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import httpx

import alerts
import ledger
import noc
import services

log = logging.getLogger(__name__)

_COMMAND_TOKEN = os.environ.get("TELEGRAM_COMMAND_BOT_TOKEN", "")
_ENABLED = bool(_COMMAND_TOKEN)
_OFFSET_FILE = noc._STATE_DIR / "tg_command_offset.json"
_ALLOWED_CHAT_ID = str(alerts.TELEGRAM_CHAT_ID)


# ---- command handlers (pure-ish: state changes via noc/alerts only) ---------

def _find_agents(query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    return [s for s in services.SERVICES if q in s["name"].lower()]


def handle_text(text: str) -> str | None:
    """Handle one incoming command line, return the reply text (or None for
    non-commands). Side effects go through noc.set_maintenance/clear_lock --
    monkeypatch-able in tests."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]  # tolerate "/status@MyBot"
    args = parts[1:]

    if cmd == "/unlock":
        matches = _find_agents(" ".join(args))
        if len(matches) != 1:
            names = ", ".join(s["name"] for s in matches) or "none"
            return f"/unlock: be specific -- {len(matches)} match(es): {names}"
        svc = matches[0]
        was_locked = bool(noc.get_status(svc["name"]) and noc.get_status(svc["name"]).get("locked"))
        noc.clear_lock(svc["name"])
        return f"\U0001f513 {svc['name']} unlocked by operator override" if was_locked \
            else f"{svc['name']} was not locked (window reset anyway)"

    if cmd == "/status":
        lines = []
        any_problem = False
        for svc in services.SERVICES:
            if not svc.get("monitor"):
                continue
            st = noc.get_status(svc["name"])
            if st is None:
                lines.append(f"\u26aa {svc['name']}: not checked yet")
                continue
            if st.get("muted"):
                mark, note = "\U0001f7e0", "monitoring paused"
            elif st.get("quarantined"):
                mark, note = "\u26d4\ufe0f", f"quarantined ({st['quarantined']})"
            elif not st["up"]:
                mark, note = "\U0001f534", "down"
            elif st.get("locked"):
                info = st.get("lock_info") or {}
                if info.get("sticky"):
                    mark, note = "\U0001f512", f"locked -- needs you ({info.get('strikes')}nd strike)"
                else:
                    mark, note = "\U0001f512", f"locked -- auto-unlock ~{info.get('unlocks_in_min', '?')}m"
            elif st.get("readiness") == "stale":
                mark = ("\U0001f7e1" if svc.get("enforced_cadence") else "\u26aa")
                note = st.get("readiness_detail") or "stale"
            else:
                mark, note = "\U0001f7e2", ""
            any_problem = any_problem or mark != "\U0001f7e2"
            lines.append(f"{mark} {svc['name']}: {note}" if note else f"{mark} {svc['name']}")
        try:
            cost = ledger.today_cost(ledger.fetch_rows(1))
            lines.append(f"\U0001f4b8 today's LLM spend: ${cost:.4f}")
        except Exception as e:  # noqa: BLE001 -- status must never raise
            lines.append(f"(today's cost unavailable: {e})")
        return "\n".join(lines)

    if cmd == "/mute":
        try:
            minutes = max(1, int(args[0]))
        except (IndexError, ValueError):
            return "usage: /mute <minutes> [agent-substring ...]"
        targets = " ".join(args[1:]).strip()
        if targets:
            matched = {s["name"] for q in targets.split() for s in _find_agents(q)}
            if not matched:
                return f"/mute: no agents match '{targets}'"
            noc.set_maintenance(minutes, scope=",".join(sorted(matched)))
            return f"\U0001f7e0 maintenance for {minutes}m: {', '.join(sorted(matched))}"
        noc.set_maintenance(minutes, scope="all")
        return f"\U0001f7e0 global maintenance for {minutes}m -- NOC actions suppressed"

    if cmd == "/unmute":
        noc.clear_maintenance()
        return "\U0001f7e2 monitoring resumed"

    if cmd in ("/start", "/help"):
        return ("commands:\n"
                "/unlock <agent> - clear a restart lock\n"
                "/status - one-glance health + today's spend\n"
                "/mute <min> [agents] - pause NOC actions\n"
                "/unmute - resume monitoring")

    return None  # unknown slash-command or plain chat: stay silent


def poll_updates() -> int:
    """One long-poll cycle of getUpdates; dispatch each message through
    handle_text and echo the reply into the chat. Returns messages handled.
    Blocking (~25s while waiting); run via asyncio.to_thread."""
    if os.environ.get("NICEGUI_USER_SIMULATION"):
        return 0  # render tests: a 25s-blocking poll wrecks test isolation
    if not _ENABLED or not alerts.is_telegram_configured():
        time.sleep(30)  # control plane not opted in -- idle quietly
        return 0
    offset = 0
    try:
        offset = json.loads(_OFFSET_FILE.read_text()).get("offset", 0)
    except (FileNotFoundError, ValueError):
        pass
    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{_COMMAND_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message"])},
            timeout=35,
        )
        if resp.status_code != 200:
            # 409 webhook conflict etc.: log once per cycle, never raise
            log.warning("commands: getUpdates returned %s: %s",
                        resp.status_code, resp.text[:150])
            return 0
        updates = resp.json().get("result", [])
    except Exception as e:  # noqa: BLE001
        log.warning("commands: getUpdates failed: %s", e)
        return 0
    handled = 0
    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != _ALLOWED_CHAT_ID:
            continue  # not the operator's chat -- ignore silently
        reply = handle_text(msg.get("text", ""))
        if reply:
            alerts.send_telegram(reply, tag="CMD", emoji="\U0001f916")
            handled += 1
    _OFFSET_FILE.write_text(json.dumps({"offset": offset}))
    return handled
