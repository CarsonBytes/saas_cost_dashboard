#!/bin/bash
# Deterministic, idempotent redeploy of the WSL2/Docker deployment (the sole
# deployment as of 2026-08-13 -- the native Windows launcher was decommissioned
# once this was verified working end-to-end). Mirrors D:\quant\scripts\
# wsl2-docker-deploy.sh's pattern exactly, minus the IB Gateway-specific steps
# (not applicable here -- no broker connection, no session-conflict handling).
#
# "Deterministic" here means: (1) the Dockerfile's base images are
# digest-pinned, so the SAME commit always produces the SAME deployed image;
# (2) this script always runs the same fixed sequence regardless of prior
# state -- rsync --delete + docker compose build + up -d converge to the
# correct result either way, so re-running this after a failed attempt is
# always safe.
#
# Invoked by .githooks/pre-push in the BACKGROUND, so `git push` itself is
# never slowed down by a rebuild. Safe to run manually too:
#   wsl -d Ubuntu -- bash /mnt/d/llm-usage-dashboard/scripts/wsl2-docker-deploy.sh
set -uo pipefail   # NOT -e: a failed step must still fall through to the
                   # log-and-exit-1 below, not abort mid-script leaving no record

LOG="/home/cap/llm-usage-dashboard-deploy.log"
DASH_URL="http://localhost:8095"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
    echo "=== deploy started $(ts) (triggered by: ${1:-manual}) ==="

    rsync -a --delete \
        --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='.env' --exclude='.nicegui' \
        --exclude='alert_state.json' --exclude='alert_settings.json' \
        --exclude='noc_state.json' \
        /mnt/d/llm-usage-dashboard/ /home/cap/llm-usage-dashboard/
    rsync_rc=$?
    if [ $rsync_rc -ne 0 ]; then
        echo "!!! rsync failed (exit $rsync_rc) -- aborting, previous deployment left running"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    cd /home/cap/llm-usage-dashboard || { echo "!!! /home/cap/llm-usage-dashboard missing after rsync"; exit 1; }

    # .env is deliberately excluded from rsync (real credentials, never
    # overwritten/deleted by a sync) -- fail loudly here instead of a
    # confusing runtime failure inside the container if it's ever missing.
    if [ ! -f .env ]; then
        echo "!!! .env missing at /home/cap/llm-usage-dashboard/.env -- aborting"
        echo "    (deliberately not synced from /mnt/d -- copy it once by hand)"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    docker compose build
    build_rc=$?
    if [ $build_rc -ne 0 ]; then
        echo "!!! docker compose build failed (exit $build_rc) -- aborting, previous image/"
        echo "    container left running untouched"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    docker compose up -d
    up_rc=$?
    if [ $up_rc -ne 0 ]; then
        echo "!!! docker compose up failed (exit $up_rc)"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    # Bounded health check -- 10 tries, 3s apart (~30s), same cadence quant's
    # own deploy script uses to verify manually.
    ok=0
    for i in $(seq 1 10); do
        if curl -sf -o /dev/null "$DASH_URL"; then
            ok=1
            break
        fi
        sleep 3
    done

    if [ "$ok" != "1" ]; then
        echo "!!! $DASH_URL not answering after ~30s"
        echo "=== deploy FAILED health check $(ts) ==="
        exit 1
    fi
    echo "=== deploy OK $(ts) -- $DASH_URL answering, container healthy ==="
} >> "$LOG" 2>&1
exit 0
