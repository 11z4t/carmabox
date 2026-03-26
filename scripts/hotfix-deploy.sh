#!/bin/bash
# IT-2466: CARMA Box Hotfix Deploy — fast deploy without full HA restart
#
# Syncs changed files to production HA, then reloads the integration.
# Thanks to IT-2466 module cache invalidation in __init__.py,
# a config entry reload now picks up file changes from disk.
#
# Usage:
#   ./scripts/hotfix-deploy.sh [--restart] [--dry-run]
#
# Options:
#   --restart   Force full HA restart instead of integration reload
#   --dry-run   Show what would be synced without executing
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_DIR/custom_components/carmabox/"
HA_HOST="hassio@192.168.5.22"
HA_TARGET="/homeassistant/custom_components/carmabox/"
# Config entry ID for carmabox (from HA .storage/core.config_entries)
CONFIG_ENTRY_ID="01KM89TAWV80X6R1SEHWG5JFFX"

FORCE_RESTART=false
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --restart) FORCE_RESTART=true ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

echo "=== CARMA Box Hotfix Deploy (IT-2466) ==="
echo "Source: $SRC"
echo "Target: $HA_HOST:$HA_TARGET"

# Step 1: Show what changed
echo ""
echo "--- Changed files ---"
CHANGES=$(rsync -avnc --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$SRC" "$HA_HOST:$HA_TARGET" 2>/dev/null | grep -E '^\S' | grep -v '^sending\|^sent\|^total\|^$\|^\./$' || true)

if [ -z "$CHANGES" ]; then
    echo "No changes detected — nothing to deploy."
    exit 0
fi
echo "$CHANGES"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "Dry run — no changes made."
    exit 0
fi

# Step 2: Sync files to HA
echo ""
echo "--- Syncing files ---"
rsync -avz --delete \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$SRC" "$HA_HOST:$HA_TARGET"

# Step 3: Clean __pycache__ on target to avoid stale .pyc
echo ""
echo "--- Cleaning __pycache__ on HA ---"
ssh "$HA_HOST" "find $HA_TARGET -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true"

# Step 4: Reload or restart
echo ""
if [ "$FORCE_RESTART" = true ]; then
    echo "--- Restarting Home Assistant (--restart flag) ---"
    ssh "$HA_HOST" "sudo sh -c 'T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
        curl -sf -X POST -H \"Authorization: Bearer \$T\" \
        http://supervisor/core/api/services/homeassistant/restart'"
    echo "Restart triggered — waiting 120s for HA to come back..."
    sleep 120
else
    echo "--- Reloading CARMA Box integration ---"
    ssh "$HA_HOST" "sudo sh -c 'T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
        curl -sf -X POST -H \"Authorization: Bearer \$T\" \
        http://supervisor/core/api/config/config_entries/entry/${CONFIG_ENTRY_ID}/reload'"
    echo "Integration reload triggered — waiting 30s..."
    sleep 30
fi

# Step 5: Verify sensor is available
echo ""
echo "--- Verifying CARMA Box ---"
DECISION=$(ssh "$HA_HOST" "sudo sh -c 'T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
    curl -sf -H \"Authorization: Bearer \$T\" \
    http://supervisor/core/api/states/sensor.carma_box_decision'" 2>/dev/null || echo '{"state":"ERROR"}')

STATE=$(echo "$DECISION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','ERROR'))" 2>/dev/null || echo "ERROR")

if [ "$STATE" = "unavailable" ] || [ "$STATE" = "ERROR" ]; then
    echo "WARNING: sensor.carma_box_decision is $STATE after reload!"
    echo "Falling back to full HA restart..."
    ssh "$HA_HOST" "sudo sh -c 'T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
        curl -sf -X POST -H \"Authorization: Bearer \$T\" \
        http://supervisor/core/api/services/homeassistant/restart'"
    echo "Full restart triggered — waiting 120s..."
    sleep 120

    # Re-verify
    DECISION=$(ssh "$HA_HOST" "sudo sh -c 'T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
        curl -sf -H \"Authorization: Bearer \$T\" \
        http://supervisor/core/api/states/sensor.carma_box_decision'" 2>/dev/null || echo '{"state":"ERROR"}')
    STATE=$(echo "$DECISION" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','ERROR'))" 2>/dev/null || echo "ERROR")

    if [ "$STATE" = "unavailable" ] || [ "$STATE" = "ERROR" ]; then
        echo "CRITICAL: CARMA Box still unavailable after full restart!"
        exit 1
    fi
fi

echo ""
echo "=== Hotfix deploy OK — sensor.carma_box_decision = $STATE ==="
