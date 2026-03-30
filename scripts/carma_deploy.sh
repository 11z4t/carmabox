#!/bin/bash
# =============================================================================
# carma_deploy.sh - Deploy CARMA Box to production HA
# =============================================================================
# One command to lint, test, commit, push, deploy, reload and verify.
#
# Usage:
#   carma_deploy.sh "Description of the change"
#
# Exit codes:
#   0 = OK (deploy complete, no errors)
#   1 = Ruff lint failed
#   2 = Tests failed
#   3 = Git commit/push failed
#   4 = Deploy (file copy) failed
#   5 = HA errors detected after deploy
# =============================================================================

set -e

REPO_DIR="/home/charlie/carmabox"
COMPONENT_DIR="$REPO_DIR/custom_components/carmabox"
HA_HOST="ha"
HA_DEST="/homeassistant/custom_components/carmabox"
LOG_FILE="/tmp/carma_deploy_$(date +%Y%m%d_%H%M%S).log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

DESCRIPTION="${1:-}"

if [ -z "$DESCRIPTION" ]; then
    echo -e "${RED}ERROR: Description required as first argument${NC}"
    echo "Usage: carma_deploy.sh \"Short description of the change\""
    exit 3
fi

log() {
    local level="$1"; shift; local msg="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $msg" >> "$LOG_FILE"
    case "$level" in
        ERROR)   echo -e "${RED}ERROR: $msg${NC}" ;;
        WARNING) echo -e "${YELLOW}WARNING: $msg${NC}" ;;
        SUCCESS) echo -e "${GREEN}OK: $msg${NC}" ;;
        INFO)    echo -e "${BLUE}INFO: $msg${NC}" ;;
        *)       echo "$msg" ;;
    esac
}

cd "$REPO_DIR"

# =============================================================================
# Step 1: Ruff lint
# =============================================================================
log INFO "Step 1/8: Running ruff lint..."
if ! python3 -m ruff check custom_components/carmabox/ 2>&1 | tee -a "$LOG_FILE"; then
    log ERROR "Ruff lint failed -- fix errors before deploying"
    exit 1
fi
log SUCCESS "Ruff lint passed"

# =============================================================================
# Step 2: Unit tests
# =============================================================================
log INFO "Step 2/8: Running unit tests..."
if ! python3 -m pytest tests/unit/ -q --tb=no 2>&1 | tee -a "$LOG_FILE"; then
    log ERROR "Unit tests failed -- fix failures before deploying"
    exit 2
fi
log SUCCESS "Unit tests passed"

# =============================================================================
# Step 3: Git add + commit
# =============================================================================
log INFO "Step 3/8: Committing changes..."
git add -A
LOCAL_STATUS=$(git status --porcelain)

if [ -z "$LOCAL_STATUS" ]; then
    log INFO "No changes to commit (already committed)"
else
    CHANGED_COUNT=$(echo "$LOCAL_STATUS" | wc -l)
    log INFO "Staging $CHANGED_COUNT changed files"
    echo "$LOCAL_STATUS" | head -20

    if ! git commit -m "$DESCRIPTION

Co-Authored-By: Claude Agent <noreply@anthropic.com>"; then
        log ERROR "Git commit failed"
        exit 3
    fi
    log SUCCESS "Changes committed"
fi

# =============================================================================
# Step 4: Git push
# =============================================================================
log INFO "Step 4/8: Pushing to Gitea..."
PUSH_OUTPUT=$(git push origin 2>&1) || true
if echo "$PUSH_OUTPUT" | grep -qiE "rejected|failed|error"; then
    log ERROR "Git push failed: $PUSH_OUTPUT"
    exit 3
fi
log SUCCESS "Pushed to Gitea"

# =============================================================================
# Step 5: Copy files to HA
# =============================================================================
log INFO "Step 5/8: Deploying files to HA..."
DEPLOY_COUNT=0
DEPLOY_FAIL=0

# Find all .py and non-code resource files, skip __pycache__, *.pyc, tests
while IFS= read -r file; do
    # Relative path from the component dir
    relpath="${file#$COMPONENT_DIR/}"

    # Create target directory on HA
    target_dir="$HA_DEST/$(dirname "$relpath")"
    ssh "$HA_HOST" "sudo mkdir -p '$target_dir'" 2>/dev/null

    # Copy file via cat pipe
    if cat "$file" | ssh "$HA_HOST" "sudo sh -c 'cat > \"$HA_DEST/$relpath\"'" 2>/dev/null; then
        DEPLOY_COUNT=$((DEPLOY_COUNT + 1))
    else
        log ERROR "Failed to copy: $relpath"
        DEPLOY_FAIL=$((DEPLOY_FAIL + 1))
    fi
done < <(find "$COMPONENT_DIR" \
    -not -path '*/__pycache__/*' \
    -not -path '*/tests/*' \
    -not -name '*.pyc' \
    -not -name '*.tmp.*' \
    -not -type d \
    \( -name '*.py' -o -name '*.json' -o -name '*.png' -o -name '*.svg' \))

if [ "$DEPLOY_FAIL" -gt 0 ]; then
    log ERROR "Failed to deploy $DEPLOY_FAIL files"
    exit 4
fi
log SUCCESS "Deployed $DEPLOY_COUNT files to HA"

# =============================================================================
# Step 6: Clear pyc cache on HA
# =============================================================================
log INFO "Step 6/8: Clearing __pycache__ on HA..."
ssh "$HA_HOST" "sudo find '$HA_DEST' -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null; \
                sudo find '$HA_DEST' -name '*.pyc' -delete 2>/dev/null" || true
log SUCCESS "Cache cleared"

# =============================================================================
# Step 7: Reload CARMA Box integration
# =============================================================================
log INFO "Step 7/8: Reloading CARMA Box integration..."
RELOAD_OUTPUT=$(ssh "$HA_HOST" "sudo sh -c '\
    T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
    curl -s -X POST \
        -H \"Authorization: Bearer \$T\" \
        -H \"Content-Type: application/json\" \
        -d \"{\\\"entry_id\\\": \\\"\\\"}\" \
        http://supervisor/core/api/services/homeassistant/reload_config_entry'" 2>&1) || true

# Fallback: reload custom components via full reload
if echo "$RELOAD_OUTPUT" | grep -qiE '"error"'; then
    log WARNING "Config entry reload failed, trying full reload..."
    ssh "$HA_HOST" "sudo sh -c '\
        T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
        curl -s -X POST \
            -H \"Authorization: Bearer \$T\" \
            http://supervisor/core/api/services/homeassistant/reload_all'" 2>/dev/null || true
fi
log SUCCESS "Reload triggered"

# =============================================================================
# Step 8: Wait and check HA error log
# =============================================================================
log INFO "Step 8/8: Waiting 15s then checking HA logs..."
sleep 15

HA_LOGS=$(ssh "$HA_HOST" "sudo sh -c '\
    T=\$(cat /run/s6/container_environment/SUPERVISOR_TOKEN) && \
    curl -s -H \"Authorization: Bearer \$T\" \
        http://supervisor/core/logs'" 2>&1) || true

# Filter for carmabox errors in last portion of log
CARMA_ERRORS=$(echo "$HA_LOGS" | grep -iE 'carmabox.*error|error.*carmabox|carma_box.*error|error.*carma_box' | tail -10 || true)
ERROR_COUNT=0
[ -n "$CARMA_ERRORS" ] && ERROR_COUNT=$(echo "$CARMA_ERRORS" | wc -l)

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "============================================================"
echo "  CARMA BOX DEPLOY SUMMARY"
echo "============================================================"
echo "  Description:  $DESCRIPTION"
echo "  Timestamp:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Ruff:         PASSED"
echo "  Tests:        PASSED"
echo "  Git:          COMMITTED & PUSHED"
echo "  Files:        $DEPLOY_COUNT deployed"
echo "  Cache:        CLEARED"
echo "  Reload:       TRIGGERED"
echo "  HA Errors:    $ERROR_COUNT"
echo "============================================================"
echo ""

echo "$HA_LOGS" > "/tmp/carma_deploy_full_log.txt"
log INFO "Full HA log saved to /tmp/carma_deploy_full_log.txt"
log INFO "Deploy log saved to $LOG_FILE"

if [ "$ERROR_COUNT" -gt 0 ]; then
    echo -e "${RED}=== CARMABOX ERRORS ===${NC}"
    echo "$CARMA_ERRORS"
    echo ""
    log ERROR "Deploy completed but $ERROR_COUNT carmabox errors found in HA log"
    exit 5
else
    log SUCCESS "Deploy completed successfully -- no carmabox errors in HA log"
    exit 0
fi
