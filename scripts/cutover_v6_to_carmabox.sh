#!/usr/bin/env bash
# =============================================================================
# CARMA Box Cutover — DEPRECATED
#
# This script is replaced by the comprehensive version in ha-config:
#   /home/charlie/workspaces/ha-config/scripts/cutover_v6_to_carmabox.sh
#
# That script handles:
#   - All 21 v6 GoodWe automations (tiered disable/enable)
#   - Atomic safe state (battery_standby + PS=20000W)
#   - CARMA Box executor toggle via config entry
#   - Full verification
#   - Automatic rollback on failure
#
# Usage:
#   cutover_v6_to_carmabox.sh cutover    — Full atomic cutover
#   cutover_v6_to_carmabox.sh rollback   — Restore v6 control
#   cutover_v6_to_carmabox.sh status     — Show current state
#   cutover_v6_to_carmabox.sh verify     — Verify consistency
#
# PLAT-941
# =============================================================================
set -euo pipefail

REAL_SCRIPT="/home/charlie/workspaces/ha-config/scripts/cutover_v6_to_carmabox.sh"

if [[ ! -x "$REAL_SCRIPT" ]]; then
    echo "ERROR: Cutover script not found at $REAL_SCRIPT"
    exit 1
fi

# Map old --rollback flag to new syntax
if [[ "${1:-}" == "--rollback" ]]; then
    exec "$REAL_SCRIPT" rollback
fi

exec "$REAL_SCRIPT" "${@}"
