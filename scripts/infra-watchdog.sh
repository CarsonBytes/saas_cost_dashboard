#!/bin/bash
# Unified container watchdog: checks all 9 monitored containers for health,
# auto-restarts unhealthy ones, and alerts via Telegram if they fail to recover.
# Replaces the quant-only docker-watchdog.sh with a single script covering
# all stacks. Runs every 1 min via cron.
#
# Cron entry (replaces the old docker-watchdog.sh line):
#   * * * * * /home/cap/infra-watchdog.sh
set -uo pipefail

LOG="/home/cap/infra-watchdog.log"
DASHBOARD_DIR="/home/cap/llm-usage-dashboard"

# Per-container counter files instead of one shared state file (ADDED 2026-10-04).
# The old design did `sed -i` and `echo >>` on a single file from a cron job that
# could overlap with its previous run. Racing those two leaves a sparse hole, and
# a hole in a text file is NUL bytes. NUL bytes are fatal for this script's logic:
# grep detects a binary file, exits 0 (match!) but prints NOTHING to the pipe, so
# `count` came back empty, `count+1` evaluated to 1, and `[ 1 -ge 3 ]` could never
# hold -- silently disabling every auto-restart while the log kept showing
# "(attempt 1)" forever. One file per counter cannot interleave, and values are
# read back through a digit filter so garbage can never reach the arithmetic.
STATE_DIR="/home/cap/.infra-watchdog-state.d"

# Restart circuit breaker (ADDED 2026-10-04). Previously a container that a
# restart did not fix was restarted again after 3 minutes, forever, with a
# Telegram alert each time -- 2026-09-26 saw event-radar and event-radar-demo
# restarted continuously for ~20 hours (~2400 alert lines in the log). Budget is
# per container per hour: after RESTART_LIMIT_PER_HOUR restarts it stops touching
# the container and sends exactly one escalation instead.
RESTART_LIMIT_PER_HOUR=${RESTART_LIMIT_PER_HOUR:-3}

LOCK_FILE=/home/cap/.infra-watchdog.lock

mkdir -p "$STATE_DIR"

# Non-blocking: if the previous run is still going (docker/curl calls can exceed
# the 1-minute cron period), drop this minute rather than interleave with it.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    flock -n 9 || exit 0
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# All containers to monitor (name:port for HTTP check)
CONTAINERS=(
    "saas-cost-dashboard:8095"
    "saas-cost-restart-proxy:none"
    "quant-dashboard-docker:18080"
    "quant-ibgateway-docker:none"
    "quant-dashboard-live-docker:18081"
    "quant-ibgateway-live-docker:none"
    "event-radar:8002"
    "event-radar-demo:8003"
    "study-app:8091"
    # ADDED 2026-10-04: these four were running (all healthy, all answering
    # GET / with 200) but absent from this list, so nothing watched them -- a
    # stopped study-demo, spendlens-app, spendlens-demo or linked-content-engine
    # would never have been noticed. study-demo sits in docker-compose's
    # ALLOWED_CONTAINERS and in the restart-proxy allow-list right next to the
    # already-watched study-app, so its omission looks like an oversight.
    "study-demo:8092"
    "spendlens-app:8093"
    "spendlens-demo:8094"
    "linked-content-engine:8096"
)

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Load Telegram credentials from the dashboard's .env
load_telegram() {
    if [ -f "$DASHBOARD_DIR/.env" ]; then
        TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "$DASHBOARD_DIR/.env" 2>/dev/null || true)
        TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "$DASHBOARD_DIR/.env" 2>/dev/null || true)
    fi
}

send_telegram() {
    local msg="$1"
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
        return 0
    fi
    curl -sf -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=🔴 [INFRA-WATCHDOG] ${msg}" \
        --max-time 10 > /dev/null 2>&1
}

# Read a counter. `tr -d -c '0-9'` is deliberate: it drops NUL bytes and any
# other corruption, so the value handed to arithmetic below is always a number.
# A missing file or an empty/garbage file reads as 0.
count_get() {
    local v=""
    [ -f "$STATE_DIR/$1" ] && v=$(tr -d -c '0-9' < "$STATE_DIR/$1" 2>/dev/null)
    echo "${v:-0}"
}

# Write a counter atomically (temp file + rename), never a read-modify-write of
# a shared file.
count_set() {
    local tmp="$STATE_DIR/$1.tmp"
    printf '%s\n' "$2" > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE_DIR/$1" 2>/dev/null
}

# Consume one restart credit for this container this hour.
#   0 -> the caller may restart now
#   1 -> budget exhausted, do NOT restart
may_restart() {
    local name="$1" hour count
    hour=$(date +%Y%m%d%H)
    if [ "$(count_get "rsth_$name")" != "$hour" ]; then
        count_set "rsth_$name" "$hour"
        count_set "rstc_$name" 0
    fi
    count=$(count_get "rstc_$name")
    if [ "$count" -ge "$RESTART_LIMIT_PER_HOUR" ]; then
        return 1
    fi
    count_set "rstc_$name" "$((count + 1))"
    return 0
}

# At most one alert per container per hour, so a container that is past its
# restart budget still announces itself once without becoming a nuisance.
escalate_once() {
    local name="$1" msg="$2" hour
    hour=$(date +%Y%m%d%H)
    [ "$(count_get "esc_$name")" = "$hour" ] && return 0
    count_set "esc_$name" "$hour"
    send_telegram "$msg"
}

load_telegram

log_lines=""

for entry in "${CONTAINERS[@]}"; do
    name="${entry%%:*}"
    check="${entry##*:}"

    # 1. Check container exists and is running
    status=$(docker inspect --format='{{.State.Status}}' "$name" 2>/dev/null || echo "missing")
    # ADDED 2026-10-04: `docker inspect --format=... <no such container>` prints
    # a bare newline to STDOUT and then exits 1 (reproducible), while command
    # substitution strips only TRAILING newlines -- so a missing container left
    # status=$'\nmissing'. That newline was written verbatim into the log and
    # split the entry, producing a bare "missing (attempt 1)" line with no
    # timestamp and no container name (85 of them are already in the log).
    # Strip CR/LF at capture, before status is compared or interpolated.
    status=$(printf '%s' "$status" | tr -d '\r\n')
    if [ "$status" != "running" ]; then
        count=$(count_get "$name")
        count=$((count + 1))
        count_set "$name" "$count"
        log_lines+="[$(ts)] $name status=$status (attempt $count)\n"
        if [ "$count" -ge 3 ]; then
            if may_restart "$name"; then
                send_telegram "$name is $status for ${count}min — restarting (attempt $(count_get "rstc_$name")/$RESTART_LIMIT_PER_HOUR this hour)"
                docker restart "$name" 2>/dev/null
                count_set "$name" 0
            else
                escalate_once "$name" "$name is $status for ${count}min and has used all $RESTART_LIMIT_PER_HOUR restarts this hour — not restarting again, needs a human"
            fi
        fi
        continue
    fi

    # 2. HTTP/TCP check (ground truth — Docker healthcheck can be stale)
    if [[ "$check" == "none" ]]; then
        # Gateway containers: only check Docker health status
        health=$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo "none")
        # Same leading-newline defect as $status above (the comparison would still
        # fail correctly, but strip it so the value is never surprising).
        health=$(printf '%s' "$health" | tr -d '\r\n')
        if [ "$health" = "unhealthy" ]; then
            count=$(count_get "$name")
            count=$((count + 1))
            count_set "$name" "$count"
            log_lines+="[$(ts)] $name unhealthy (attempt $count)\n"
            if [ "$count" -ge 2 ]; then
                if may_restart "$name"; then
                    send_telegram "$name unhealthy for ${count}min — restarting (attempt $(count_get "rstc_$name")/$RESTART_LIMIT_PER_HOUR this hour)"
                    docker restart "$name" 2>/dev/null
                    count_set "$name" 0
                else
                    escalate_once "$name" "$name has been unhealthy for ${count}min and has used all $RESTART_LIMIT_PER_HOUR restarts this hour — not restarting again, needs a human"
                fi
            fi
        else
            count_set "$name" 0
        fi
        continue
    fi

    if [[ "$check" == *"tcp" ]]; then
        port="${check%tcp}"
        if ! python3 -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('127.0.0.1', $port)); s.close()" 2>/dev/null; then
            count=$(count_get "$name")
            count=$((count + 1))
            count_set "$name" "$count"
            log_lines+="[$(ts)] $name port $port not responding (attempt $count)\n"
            if [ "$count" -ge 3 ]; then
                # No docker restart here (unchanged): the tcp branch only ever
                # reported. Gated so it announces once per hour, not every 3min.
                escalate_once "$name" "$name port $port unreachable for ${count}min"
                count_set "$name" 0
            fi
            continue
        fi
    else
        if ! curl -4 -sf -o /dev/null --max-time 15 "http://127.0.0.1:$check" 2>/dev/null; then
            count=$(count_get "$name")
            count=$((count + 1))
            count_set "$name" "$count"
            log_lines+="[$(ts)] $name HTTP :$check not responding (attempt $count)\n"
            if [ "$count" -ge 3 ]; then
                if may_restart "$name"; then
                    send_telegram "$name HTTP :$check unreachable for ${count}min — restarting (attempt $(count_get "rstc_$name")/$RESTART_LIMIT_PER_HOUR this hour)"
                    docker restart "$name" 2>/dev/null
                    count_set "$name" 0
                else
                    escalate_once "$name" "$name HTTP :$check has been unreachable for ${count}min and has used all $RESTART_LIMIT_PER_HOUR restarts this hour — not restarting again, needs a human"
                fi
            fi
            continue
        fi
    fi

    # All good — reset counter
    count_set "$name" 0
done

# Write log (rotate at 1MB)
if [ -n "$log_lines" ]; then
    # printf, not `echo -e`: log_lines already ends in a literal \n and echo -e
    # appended another, so every entry was followed by a blank line -- 3194 of the
    # log's 8260 lines were blank, which is also what pushed it towards the 1MB
    # rotation threshold and threw away real history.
    printf '%b' "$log_lines" >> "$LOG"
fi
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
