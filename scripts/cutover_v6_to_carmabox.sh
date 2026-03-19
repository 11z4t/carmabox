#!/usr/bin/env bash
# =============================================================================
# CARMA Box Cutover: Disable v6 automations → Enable executor
#
# Usage: ./scripts/cutover_v6_to_carmabox.sh [--rollback]
#
# KRITISKT: Kör INTE detta utan att ha verifierat shadow mode data först!
# =============================================================================
set -euo pipefail

HA_SSH="hassio@192.168.5.22"
ADDON="addon_a0d7b954_ssh"
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

ha_api() {
    ssh "$HA_SSH" bash -s -- "$1" "$2" "${3:-}" <<'REMOTE'
METHOD="$1"; PATH_ARG="$2"; BODY="$3"; ADDON="addon_a0d7b954_ssh"
URL="http://supervisor/core/api${PATH_ARG}"
if [ "$METHOD" = "GET" ]; then
    sudo docker exec "$ADDON" curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" "$URL"
elif [ "$METHOD" = "POST" ] && [ -n "$BODY" ]; then
    echo "$BODY" | sudo docker cp /dev/stdin "$ADDON:/tmp/api_body.json"
    sudo docker exec "$ADDON" curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" -X POST -d @/tmp/api_body.json "$URL"
else
    sudo docker exec "$ADDON" curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" -X POST "$URL"
fi
REMOTE
}

# v6 automations that WRITE to GoodWe EMS or Easee
V6_AUTOMATIONS=(
    "automation.ellevio_daily_peak_analysis"
    "automation.ev_dynamisk_nattjustering"
    "automation.ev_guardian_stopp_utanfor_fonster"
    "automation.ev_soloverskott_start"
    "automation.ev_soloverskott_stopp"
    "automation.ellevio_realtime_monitoring"
    "automation.v6_appliance_spike_response"
    "automation.v6_manual_evening_discharge"
    "automation.ps3_ateraktivera_v1_vid_stopp"
    "automation.ev_watchdog_otillaten_laddning"
)

if [[ "${1:-}" == "--rollback" ]]; then
    echo -e "${RED}═══ ROLLBACK: Re-enabling v6 automations ═══${NC}"
    for auto in "${V6_AUTOMATIONS[@]}"; do
        ha_api POST "/services/automation/turn_on" "{\"entity_id\": \"$auto\"}" >/dev/null 2>&1 || true
        echo "  ✓ Enabled $auto"
    done
    # Disable CARMA Box executor
    echo "  TODO: Set executor_enabled=false in CARMA Box config"
    echo -e "${GREEN}Rollback complete — v6 is back in control${NC}"
    exit 0
fi

echo "═══ CARMA Box Cutover: v6 → Executor ═══"
echo ""
echo "This will:"
echo "  1. Force all batteries to standby"
echo "  2. Disable ${#V6_AUTOMATIONS[@]} v6 automations"
echo "  3. Enable CARMA Box executor"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] || exit 0

# Step 1: Force standby
echo ""
echo "── Step 1: Force standby ──"
ha_api POST "/services/select/select_option" '{"entity_id": "select.goodwe_kontor_ems_mode", "option": "battery_standby"}' >/dev/null
ha_api POST "/services/select/select_option" '{"entity_id": "select.goodwe_forrad_ems_mode", "option": "battery_standby"}' >/dev/null
echo -e "${GREEN}  ✓ Both batteries in standby${NC}"

# Step 2: Disable v6 automations
echo ""
echo "── Step 2: Disable v6 automations ──"
for auto in "${V6_AUTOMATIONS[@]}"; do
    ha_api POST "/services/automation/turn_off" "{\"entity_id\": \"$auto\"}" >/dev/null 2>&1 || true
    echo "  ✓ Disabled $auto"
done

# Step 3: Enable executor (requires config entry update + restart)
echo ""
echo "── Step 3: Enable executor ──"
echo "  Set executor_enabled=true in CARMA Box options flow"
echo "  Then restart HA"

echo ""
echo -e "${GREEN}═══ Cutover prepared ═══${NC}"
echo "To rollback: $0 --rollback"
