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
STATE_FILE="/home/cap/.infra-watchdog-state"
DASHBOARD_DIR="/home/cap/llm-usage-dashboard"

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

# Read/restart counter for a container
get_fail_count() {
    local name="$1"
    if [ -f "$STATE_FILE" ]; then
        grep "^${name}=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo 0
    else
        echo 0
    fi
}

set_fail_count() {
    local name="$1" count="$2"
    if [ -f "$STATE_FILE" ]; then
        sed -i "/^${name}=/d" "$STATE_FILE" 2>/dev/null
    fi
    echo "${name}=${count}" >> "$STATE_FILE"
}

load_telegram

log_lines=""

for entry in "${CONTAINERS[@]}"; do
    name="${entry%%:*}"
    check="${entry##*:}"

    # 1. Check container exists and is running
    status=$(docker inspect --format='{{.State.Status}}' "$name" 2>/dev/null || echo "missing")
    if [ "$status" != "running" ]; then
        count=$(get_fail_count "$name")
        count=$((count + 1))
        set_fail_count "$name" "$count"
        log_lines+="[$(ts)] $name status=$status (attempt $count)\n"
        if [ "$count" -ge 3 ]; then
            send_telegram "$name is $status for ${count}min — restarting"
            docker restart "$name" 2>/dev/null
            set_fail_count "$name" 0
        fi
        continue
    fi

    # 2. HTTP/TCP check (ground truth — Docker healthcheck can be stale)
    if [[ "$check" == "none" ]]; then
        # Gateway containers: only check Docker health status
        health=$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo "none")
        if [ "$health" = "unhealthy" ]; then
            count=$(get_fail_count "$name")
            count=$((count + 1))
            set_fail_count "$name" "$count"
            log_lines+="[$(ts)] $name unhealthy (attempt $count)\n"
            if [ "$count" -ge 2 ]; then
                send_telegram "$name unhealthy for ${count}min — restarting"
                docker restart "$name" 2>/dev/null
                set_fail_count "$name" 0
            fi
        else
            set_fail_count "$name" 0
        fi
        continue
    fi

    if [[ "$check" == *"tcp" ]]; then
        port="${check%tcp}"
        if ! python3 -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('127.0.0.1', $port)); s.close()" 2>/dev/null; then
            count=$(get_fail_count "$name")
            count=$((count + 1))
            set_fail_count "$name" "$count"
            log_lines+="[$(ts)] $name port $port not responding (attempt $count)\n"
            if [ "$count" -ge 3 ]; then
                send_telegram "$name port $port unreachable for ${count}min"
                set_fail_count "$name" 0
            fi
            continue
        fi
    else
        if ! curl -4 -sf -o /dev/null --max-time 15 "http://127.0.0.1:$check" 2>/dev/null; then
            count=$(get_fail_count "$name")
            count=$((count + 1))
            set_fail_count "$name" "$count"
            log_lines+="[$(ts)] $name HTTP :$check not responding (attempt $count)\n"
            if [ "$count" -ge 3 ]; then
                send_telegram "$name HTTP :$check unreachable for ${count}min — restarting"
                docker restart "$name" 2>/dev/null
                set_fail_count "$name" 0
            fi
            continue
        fi
    fi

    # All good — reset counter
    set_fail_count "$name" 0
done

# Write log (rotate at 1MB)
if [ -n "$log_lines" ]; then
    echo -e "$log_lines" >> "$LOG"
fi
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
