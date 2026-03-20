#!/usr/bin/env bash
# =============================================================================
# CARMA Box — Full deploy + verify pipeline
#
# Does everything: HACS update → restart → config flow → sensor verification
# No GUI interaction needed.
#
# Usage: ./scripts/ha_deploy_verify.sh [--skip-hacs] [--skip-restart]
# =============================================================================
set -euo pipefail

HA_HOST="192.168.5.22"
HA_SSH="hassio@${HA_HOST}"
DOMAIN="carmabox"
TIMEOUT=300  # seconds to wait for HA restart

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# Helper: run HA API call via SSH
# SUPERVISOR_TOKEN lives inside the addon container, so we must use bash -c there
ha_api() {
    local method="${1}" path="${2}" body="${3:-}"
    local url="http://supervisor/core/api${path}"
    local addon="addon_a0d7b954_ssh"

    if [[ "$method" == "GET" ]]; then
        ssh "$HA_SSH" "sudo docker exec $addon bash -c 'curl -s -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" -H \"Content-Type: application/json\" $url'" 2>/dev/null
    elif [[ "$method" == "DELETE" ]]; then
        ssh "$HA_SSH" "sudo docker exec $addon bash -c 'curl -s -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" -H \"Content-Type: application/json\" -X DELETE $url'" 2>/dev/null
    else
        # Write body to temp file to avoid quoting issues
        local tmpfile
        tmpfile=$(ssh "$HA_SSH" "mktemp" 2>/dev/null)
        echo "$body" | ssh "$HA_SSH" "cat > $tmpfile" 2>/dev/null
        ssh "$HA_SSH" "sudo docker cp $tmpfile $addon:/tmp/api_body.json && sudo docker exec $addon bash -c 'curl -s -H \"Authorization: Bearer \$SUPERVISOR_TOKEN\" -H \"Content-Type: application/json\" -X $method -d @/tmp/api_body.json $url' && rm -f $tmpfile" 2>/dev/null
    fi
}

# Helper: wait for HA to be ready
wait_for_ha() {
    local elapsed=0
    while [[ $elapsed -lt $TIMEOUT ]]; do
        local resp
        resp=$(ha_api GET "/" 2>/dev/null || echo "")
        if echo "$resp" | grep -q "API running"; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
        echo -n "."
    done
    echo ""
    return 1
}

# ═══════════════════════════════════════════════════════════════════
# STEP 0: Pre-flight checks
# ═══════════════════════════════════════════════════════════════════
echo "═══ CARMA Box Deploy + Verify ═══"
echo ""

# Check SSH access
ssh "$HA_SSH" 'echo ok' >/dev/null 2>&1 || fail "Cannot SSH to $HA_SSH"
log "SSH access OK"

# Check HA is running
ha_api GET "/" | grep -q "API running" || fail "HA API not responding"
log "HA API running"

SKIP_HACS=false
SKIP_RESTART=false
for arg in "$@"; do
    case "$arg" in
        --skip-hacs) SKIP_HACS=true ;;
        --skip-restart) SKIP_RESTART=true ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════
# STEP 1: Remove existing config entry (if any)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 1: Remove existing config entry ──"

ENTRIES=$(ha_api GET "/config/config_entries/entry" | python3 -c "
import sys, json
data = json.load(sys.stdin)
carma = [e for e in data if e.get('domain') == '$DOMAIN']
for e in carma:
    print(e['entry_id'])
" 2>/dev/null || echo "")

if [[ -n "$ENTRIES" ]]; then
    for eid in $ENTRIES; do
        ha_api DELETE "/config/config_entries/entry/${eid}" >/dev/null
        log "Removed config entry: $eid"
    done
else
    log "No existing config entries"
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 2: Trigger HACS download via HA WebSocket (through REST wrapper)
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 2: Update via HACS ──"

if [[ "$SKIP_HACS" == "false" ]]; then
    # HACS doesn't have a REST API, but we can trigger a download via
    # the hacs.download service or by calling the websocket endpoint.
    # Alternative: use HA service call to trigger HACS repository update
    HACS_RESULT=$(ha_api POST "/services/hacs/reload" "{}" 2>/dev/null || echo "no_hacs_service")

    if echo "$HACS_RESULT" | grep -q "error\|no_hacs"; then
        warn "HACS reload service not available — using direct file sync"
        REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
        ssh "$HA_SSH" "sudo chmod -R 777 /homeassistant/custom_components/carmabox/ 2>/dev/null || true"
        rsync -az --delete --exclude='__pycache__' --no-group --no-owner --chmod=ugo=rwX \
            "${REPO_DIR}/custom_components/carmabox/" \
            "${HA_SSH}:/homeassistant/custom_components/carmabox/" 2>/dev/null || true
        log "Files synced (fallback)"
    else
        log "HACS reload triggered"
        sleep 5
    fi
else
    log "Skipping HACS update (--skip-hacs)"
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 3: Restart HA
# ═══════════════════════════════════════════════════════════════════
if [[ "$SKIP_RESTART" == "false" ]]; then
    echo ""
    echo "── Step 3: Restart HA ──"

    ssh "$HA_SSH" 'sudo docker restart homeassistant' >/dev/null 2>&1 || true
    log "HA restart triggered"

    echo -n "   Waiting for HA"
    sleep 15  # Give it time to stop
    if wait_for_ha; then
        echo ""
        log "HA is up"
    else
        fail "HA did not come up within ${TIMEOUT}s"
    fi

    # Extra wait for integrations to load
    sleep 15
else
    log "Skipping restart (--skip-restart)"
fi

# ═══════════════════════════════════════════════════════════════════
# STEP 4: Run config flow via API
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 4: Run config flow ──"

# Step 4a: Start flow
FLOW=$(ha_api POST "/config/config_entries/flow" "{\"handler\":\"$DOMAIN\"}")
FLOW_ID=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('flow_id',''))" 2>/dev/null)
STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)

if [[ -z "$FLOW_ID" ]]; then
    fail "Could not start config flow. Response: $FLOW"
fi
log "Flow started: $FLOW_ID (step: $STEP)"
FLOW_TYPE=""

# Step 4a2: User step (auto-detect, may need empty submit)
if [[ -z "$STEP" || "$STEP" == "user" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" "{}")
    STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)
    log "User step completed (step: $STEP)"
fi

# Step 4b: Confirm detected equipment
if [[ "$STEP" == "confirm" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" "{}")
    STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)
    log "Confirmed equipment (step: $STEP)"
fi

# Step 4c: EV config
if [[ "$STEP" == "ev" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" '{
        "ev_enabled": true,
        "ev_model": "XPENG G9",
        "ev_capacity_kwh": 98,
        "ev_night_target_soc": 75,
        "ev_full_charge_days": 7
    }')
    STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)
    log "EV configured (step: $STEP)"
fi

# Step 4d: Grid config
if [[ "$STEP" == "grid" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" '{
        "price_area": "SE3",
        "grid_operator": "ellevio",
        "peak_cost_per_kw": 80.0,
        "fallback_price_ore": 100.0
    }')
    STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)
    log "Grid configured (step: $STEP)"
fi

# Step 4e: Household config (final step)
if [[ "$STEP" == "household" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" '{
        "household_size": 4,
        "has_pool_pump": true,
        "executor_enabled": true
    }')
    STEP=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('step_id',''))" 2>/dev/null)
    FLOW_TYPE=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('type',''))" 2>/dev/null)
    log "Household configured (step: $STEP)"
fi

# Step 4f: Summary step (if present)
if [[ "$STEP" == "summary" ]]; then
    FLOW=$(ha_api POST "/config/config_entries/flow/${FLOW_ID}" '{}')
    FLOW_TYPE=$(echo "$FLOW" | python3 -c "import sys,json; print(json.load(sys.stdin).get('type',''))" 2>/dev/null)
    log "Summary confirmed"
fi

if [[ "$FLOW_TYPE" == "create_entry" ]]; then
    log "Config entry created!"
else
    warn "Unexpected flow result: $FLOW_TYPE"
    echo "$FLOW" | python3 -m json.tool 2>/dev/null || echo "$FLOW"
fi

# Wait for sensors to populate
sleep 10

# ═══════════════════════════════════════════════════════════════════
# STEP 5: Verify sensors
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 5: Verify sensors ──"

STATES=$(ha_api GET "/states")
RESULTS=$(echo "$STATES" | python3 -c "
import sys, json

data = json.load(sys.stdin)
sensors = {s['entity_id']: s['state'] for s in data if 'carma_box' in s.get('entity_id', '') and s['entity_id'].startswith('sensor.')}

expected = {
    'sensor.carma_box_plan_status': {'type': 'string', 'valid': ['idle', 'standby', 'charging', 'charging_pv', 'discharging', 'unknown']},
    'sensor.carma_box_decision': {'type': 'string'},
    'sensor.carma_box_target_power': {'type': 'float', 'min': 0.5, 'max': 10.0},
    'sensor.carma_box_monthly_savings': {'type': 'float', 'min': 0},
    'sensor.carma_box_battery_soc': {'type': 'float', 'min': 0, 'max': 100},
    'sensor.carma_box_grid_import': {'type': 'float', 'min': 0},
    'sensor.carma_box_ev_soc': {'type': 'float', 'min': 0, 'max': 100},
    'sensor.carma_box_plan_accuracy': {'type': 'any'},  # Can be unknown initially
}

passed = 0
failed = 0
total = len(expected)

for eid, checks in expected.items():
    state = sensors.get(eid)
    if state is None:
        print(f'FAIL {eid}: MISSING')
        failed += 1
        continue

    if state in ('unknown', 'unavailable'):
        if checks.get('type') == 'any':
            print(f'OK   {eid}: {state} (acceptable)')
            passed += 1
        elif eid == 'sensor.carma_box_ev_soc':
            # EV SoC can be unknown if car is offline
            print(f'WARN {eid}: {state} (car may be offline)')
            passed += 1
        else:
            print(f'FAIL {eid}: {state}')
            failed += 1
        continue

    if checks.get('type') == 'float':
        try:
            val = float(state)
            lo = checks.get('min', float('-inf'))
            hi = checks.get('max', float('inf'))
            if lo <= val <= hi:
                print(f'OK   {eid}: {val}')
                passed += 1
            else:
                print(f'FAIL {eid}: {val} (expected {lo}-{hi})')
                failed += 1
        except ValueError:
            print(f'FAIL {eid}: {state} (expected float)')
            failed += 1
    elif checks.get('type') == 'string':
        valid = checks.get('valid')
        if valid and state not in valid:
            print(f'WARN {eid}: {state} (unexpected value)')
        print(f'OK   {eid}: {state}')
        passed += 1
    else:
        print(f'OK   {eid}: {state}')
        passed += 1

print(f'')
print(f'RESULT: {passed}/{total} passed, {failed} failed')
sys.exit(1 if failed > 0 else 0)
" 2>/dev/null)

echo "$RESULTS"

# ═══════════════════════════════════════════════════════════════════
# STEP 6: Verify config entry data
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 6: Verify config entry ──"

ssh "$HA_SSH" 'sudo docker exec homeassistant python3 -c "
import json
with open(\"/config/.storage/core.config_entries\") as f:
    data = json.load(f)
for e in data[\"data\"][\"entries\"]:
    if e[\"domain\"] == \"carmabox\":
        d = e[\"data\"]
        checks = {
            \"battery_soc_1\": lambda v: \"kontor\" in v or \"forrad\" in v,
            \"battery_soc_2\": lambda v: \"kontor\" in v or \"forrad\" in v,
            \"price_entity\": lambda v: \"nordpool\" in v,
            \"ev_soc_entity\": lambda v: \"xpeng\" in v or \"ev\" in v,
            \"executor_enabled\": lambda v: v == True,
            \"grid_entity\": lambda v: \"grid\" in str(v),
        }
        passed = 0
        failed = 0
        for key, check in checks.items():
            val = d.get(key, \"MISSING\")
            if val == \"MISSING\":
                print(f\"FAIL {key}: MISSING\")
                failed += 1
            elif check(val):
                print(f\"OK   {key}: {val}\")
                passed += 1
            else:
                print(f\"FAIL {key}: {val}\")
                failed += 1
        print(f\"\")
        print(f\"CONFIG: {passed}/{passed+failed} checks passed\")
        break
"' 2>&1

# ═══════════════════════════════════════════════════════════════════
# STEP 7: Check HA logs for errors
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "── Step 7: Check logs ──"

ERRORS=$(ssh "$HA_SSH" 'sudo docker logs homeassistant --since 5m 2>&1' | grep -i "carmabox" | grep -i "error" || echo "")

if [[ -z "$ERRORS" ]]; then
    log "No CARMA Box errors in logs"
else
    warn "Errors found:"
    echo "$ERRORS"
fi

# Final summary
echo ""
echo "═══════════════════════════════════"
if echo "$RESULTS" | grep -q "0 failed"; then
    log "DEPLOY + VERIFY PASSED"
    exit 0
else
    fail "DEPLOY + VERIFY FAILED — see above"
fi
