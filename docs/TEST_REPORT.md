# TEST REPORT — CARMA Box

**Generated:** 2026-04-02
**Version:** v5.x (post-hardening)

## Summary

| Metric | Value |
|--------|-------|
| Total tests | 2755 |
| Unit tests | 2727 |
| Scenario tests | 28 (5 scenarios: B, C, D, E, F) |
| Pass rate | 100% |
| Coverage (total) | 92% |
| Coverage (core/) | 96% |
| Coverage (optimizer/) | 99% |
| Ruff errors | 0 |

## Test Categories

| Category | Count | Description |
|----------|-------|-------------|
| Coordinator | ~800 | Main loop, watchdog W1-W8, plan generation |
| Safety Guard | 51 | Discharge/charge safety checks, temp limits |
| Grid Guard | 95 | INV-1 to INV-5, breach detection, projection |
| Planner | 120+ | Price arbitrage, solar floor, night reserve |
| Night EV SM | 22 | State machine: IDLE→RAMP→CHARGING→PAUSE→DEPLETED |
| Battery Balancer | 40+ | Proportional discharge, SoC/temp weighting |
| Adapters | 30+ | GoodWe write-verify, Easee enable/disable |
| Scenarios | 28 | End-to-end: sunny day, dishwasher, restart, adapter fail, crosscharge |
| Law Guardian | 25+ | LAG 1-7 enforcement |
| Savings/ROI | 30+ | Cost tracking, peak shaving, what-if |

## Coverage Gaps

| Module | Coverage | Gap Reason |
|--------|----------|------------|
| coordinator.py | ~75% | 7000 LOC monolith, HA-dependent paths |
| execution_engine.py | 67% | Integration-heavy, requires HA services |
| coordinator_bridge.py | ~70% | Bridge to core modules, async HA calls |
| config_flow.py | ~60% | UI wizard, hard to unit test |

## Safety Tests

- SafetyGuard BLOCK: discharge below min_soc, during export, cold temp
- W1: Export correction (+ night EV skip)
- W4b: Emergency EV stop on battery depleted
- W6: Absolute grid guard (+ night EV discharge increase)
- W7: EV stuck detection (6h without SoC change)
- W8: Battery SoC imbalance alert (>15%)
- INV-2: Crosscharge detection and forced standby
- INV-3: fast_charging + discharge_pv prevention

## Named Constants Audit

All thresholds use named constants from `const.py`:
- `BATTERY_FULL_HYSTERESIS_PCT = 99`
- `SOC_IMBALANCE_THRESHOLD_PCT = 15`
- `CROSSCHARGE_DETECTION_THRESHOLD_W = 200`
- `PV_ACTIVE_THRESHOLD_W = 200`
- `LUX_DAYLIGHT = 5000`, `LUX_DARK = 500`
- `CONSECUTIVE_ERROR_LOG_INTERVAL = 10`
- `DEFAULT_PEAK_COST_PER_KW = 80.0`
- `APPLIANCE_PAUSE_THRESHOLD_W = 500`
