# Changelog

All notable changes to CARMA Box are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [4.8.1] — 2026-04-02

### Added
- **PLAT-1198** `AuditEntry` frozen dataclass + `AuditLog` ring-buffer (max 200 entries) for forensic reconstruction of all hardware write commands. Exposed via diagnostics (`audit.total_entries` + `audit.recent`).
- **PLAT-1040** Watchdog W6: EV stuck detection — if EV is charging but SoC is unchanged for 6 hours, CARMA automatically stops the charger.

### Fixed
- **PLAT-1040** `GoodWeAdapter.set_ems_mode()` now zeros `ems_power_limit` when switching to `charge_pv`, `battery_standby` or `auto` modes. Previously the inverter kept the old non-zero limit and continued grid-charging autonomously even when `grid_charge_allowed=off`.

---

## [4.8.0] — 2026-04-01

### Added
- **PLAT-1200** Consolidated appliance constants (`APPLIANCE_*`) aligned with VM 900 naming convention.
- **PLAN-03** Night EV state machine — blocks battery discharge during active night EV charging.
- **NEV-01** `NightEvState` constants and ramp-step configuration for overnight EV scheduling.

### Changed
- Night EV logic: `night_ev_keep` command now emitted every active cycle (previously skipped on re-entry).

---

## [4.7.0] — 2026-03-28

### Added
- **ML-01/03/05** Hourly predictor: add-sample None-guard, MIN_TRAINING_THRESHOLD (24 samples), ML vs static profile logging.
- **PLAN-01** EV SoC stored as unix-timestamp — survives HA restart.
- **PLAN-02** PV surplus → charge action with full test coverage.
- **PLAT-1146** mypy `--strict` fixes across all `core/` and `optimizer/` modules.
- **PLAT-1162** `GridGuardResult` float fields default to `0.0`; `ACTION_LADDER_HYSTERESIS_S` constant extracted.

### Fixed
- **ML-00** `predict_24h()` returned `None` when model was trained — missing `else` branch.
- **PLAT-1159** `_project()` elapsed calculation used `minute` as divisor instead of `max(1, minute)`.
- **PLAT-1141** Pre-correction false positive in INV-2 rule (regression fixed).
- **PLAT-1166** `night_ev_keep` command missing `elif` — emitted every active cycle now.

---

## [4.6.0] — 2026-03-22

### Added
- **PLAT-1140** Extracted `commands.py` and `state_manager.py` as standalone core modules.
- **PLAT-1141** `ExecutionEngine` extracted from `coordinator.py`; `_cmd_grid_charge` moved to `core/commands.py`.
- **PLAT-1095** `GridGuard._accumulated_viktat_wh` persisted via HA `Store` — survives restarts.
- **QUALITY-GUARD-V2** All `bare except: pass` blocks replaced with structured logging; 7 quality-guard tests added.
- **PLAT-1158** `exc_info=True` added to all `_LOGGER.error` calls in exception handlers.
- **PLAT-1051** Magic literal `6` (EV amps) replaced with `DEFAULT_EV_MIN_AMPS` constant throughout.
- **PLAT-1138** `.gitignore` extended with PRE-01 artifact patterns.

### Changed
- **PLAT-1144** `_USE_BRIDGE` feature flag removed — always uses legacy coordinator.
- **PLAT-1145** Redundant `import time` statements consolidated in `coordinator.py`.

### Fixed
- **PLAT-1161** Edge case: `cold_lock_temp_c=0.0` previously treated as falsy — regression test added.
- **PLAT-1157** `sys.modules` hack removed; HA native reload now handles module re-import cleanly.

---

## [4.5.0] — 2026-03-15

### Added
- **PLAT-1130** EXP-EPIC-SWEEP edge-case tester covering EXP-01, EXP-04, EXP-08 export scenarios.
- **PLAT-1080** Coverage batch 25/26: coordinator auto-detect paths, climate exception handlers, miner control paths.
- Price fallback: Tibber as secondary price source when Nordpool is offline.
- `PLAN-DISPLAY` shows all 24 plan hours + EV absolute SoC (not delta).

### Fixed
- **PLAT-1095** `hasattr` guard for `_grid_guard` in `_async_save_runtime` (KeyError on first run).
- **P0-FIX** EV never charged: `ev_enabled` and `executor_enabled` now default `True`.
- **FIX** Planner `price_entity` defaulted to wrong sensor — corrected to Nordpool entity.

---

## [4.0.0] — 2026-02-20

### Added
- **Sprint 3 (PLAT-877–886)** SafetyGuard on ALL hardware commands; temperature integration; SoC bounds enforcement; daily reset logic. 9/9 safety checks green.
- **PLAT-881/885** Lovelace v2.0 dashboard + adapter integration + repair flows + write-verify.
- **PLAT-883/884** Diagnostics platform (`async_get_config_entry_diagnostics`) + repairs panel; removed hardcoded battery sizes.
- **PLAT-882** Sensors refactored to `EntityDescription` pattern (Shelly-standard, 28 sensors).
- Savings sensor, CARMA Hub MQTT/WSS, savings report card, SafetyGuard (9/9 rules).

### Changed
- **Sprint 2** EV strategy, season mode, grid charge, ABC adapter interfaces.

---

## [3.0.0] — 2026-01-15

### Added
- **PLAT-816/817** Planner integration: coordinator generates real 24-hour plan from Nordpool + Solcast.
- **PLAT-816** Solcast adapter — 100% coverage, 13 tests; 2-day offline reserve.
- **PLAT-817** `grid_logic.py` — target calculation, PV reserve, season mode.
- **PLAT-813** Nordpool + Solcast adapter wired into coordinator.

---

## [2.0.0] — 2025-12-10

### Added
- **CARMA-2** Sprint 1: GoodWe, Easee, Nordpool adapters + 6 initial sensors.
- **CARMA-3** Config flow + options flow + translations (sv/en).
- **CARMA-6–13** Optimizer 100% coverage, coordinator + sensors 98–100%, adapters 100%.
- **CARMA-14/15** 9 end-to-end Playwright tests — all green.
- Pre-commit quality hook (ruff + format + pyc cleanup + unit smoke).

---

## [1.0.0] — 2025-11-01

### Added
- **CARMA-1** Initial HACS scaffold: coordinator, safety guard, models, 20 tests.
- Domain `carmabox`, HA minimum 2024.4.0, Python ≥ 3.12.
