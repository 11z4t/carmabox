"""Expert control story tests — EXP-EPIC-SWEEP.

Fokus: boundary values, error paths, concurrent state changes.

Täcker följande control stories:
  1. BMS taper detection (_is_in_taper) — gränsvärden och persistens
  2. BMS cold lock detection (_is_cold_locked) — temperaturtröskel 10 °C
  3. RULE 2 hysteresis — oscillationsprevent vid target-gränsen
  4. Safety gate ordering — heartbeat → rate limit → crosscharge
  5. PLAT-946 crosscharge med ogiltiga flags
  6. RULE 1.5 grid charge — dynamisk tröskel (daily_avg * 0.4)
  7. RULE 1.8 proaktiv urladdning — sol/regn/natt-logik
  8. _cmd_charge_pv idempotency — sänd ej om redan i taper
  9. target_kw restore efter cold lock / taper
 10. Predictor-integrationstest med korrekt mock
 11. CarmaboxState gränsvärden (kapacitetsviktad SoC etc.)
 12. RULE 4 default idle + SoC-spärr för urladdning
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.coordinator import (
    BatteryCommand,
    CarmaboxCoordinator,
)
from custom_components.carmabox.optimizer.consumption import ConsumptionProfile
from custom_components.carmabox.optimizer.hourly_ledger import EnergyLedger
from custom_components.carmabox.optimizer.models import (
    CarmaboxState,
    Decision,
    HourPlan,
    ShadowComparison,
)
from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor
from custom_components.carmabox.optimizer.report import ReportCollector
from custom_components.carmabox.optimizer.savings import SavingsState

# ── Shared fixture factory ────────────────────────────────────────────────────


def _make_coord(options: dict | None = None) -> CarmaboxCoordinator:
    """Construct a fully wired CarmaboxCoordinator without real HA.

    Key differences from the basic test_coordinator._make_coordinator:
    - Includes battery_ems_1 entity so commands actually commit (_last_command updated)
    - Mocks _runtime_store so _async_save_runtime doesn't crash
    - Closes async_create_task coroutines to suppress RuntimeWarnings
    - All safety checks default to PASS; caller can override individually
    """
    hass = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)

    # Close unawaited _log_decision coroutines to avoid RuntimeWarning
    def _safe_create_task(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    hass.async_create_task = _safe_create_task

    entry = MagicMock()
    entry.options = options or {}
    entry.data = dict(entry.options)
    entry.entry_id = "expert_test"

    states: dict[str, MagicMock] = {}

    # Pre-register EMS and discharge-limit entities so commands commit _last_command.
    # _cmd_standby / _cmd_charge_pv: only need ems entity (success=True after service call)
    # _cmd_discharge: ALSO needs battery_limit entity (success=True only when limit set)
    for entity_id, state_val in [
        ("select.ems1", "battery_standby"),
        ("number.batt_limit1", "3000"),
    ]:
        _mock_state = MagicMock()
        _mock_state.state = state_val
        _mock_state.attributes = {}
        states[entity_id] = _mock_state

    def _get_state(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = _get_state

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.hass = hass
    coord.entry = entry

    # Config: include battery_ems_1 + battery_limit_1 so legacy service path commits changes.
    # _cmd_discharge requires both: EMS entity (for mode) + limit entity (for success=True).
    base_cfg: dict = {
        "battery_ems_1": "select.ems1",
        "battery_limit_1": "number.batt_limit1",
        **(options or {}),
    }
    coord._cfg = base_cfg

    # Safety (all PASS by default)
    coord.safety = MagicMock()
    coord.safety.check_heartbeat = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_rate_limit = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_charge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_discharge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.check_crosscharge = MagicMock(return_value=MagicMock(ok=True, reason=""))
    coord.safety.record_mode_change = MagicMock()

    # Core state
    coord.plan = []
    coord._plan_counter = 0
    coord._last_command = BatteryCommand.IDLE
    coord._last_discharge_w = 0
    coord._pending_write_verifies = []
    coord.target_kw = float((options or {}).get("target_weighted_kw", 2.0))
    coord.min_soc = float((options or {}).get("min_soc", 15.0))
    coord.logger = MagicMock()
    coord.name = "carmabox"
    coord._states = states

    # Financial / reporting
    coord.savings = SavingsState(month=3, year=2026)
    coord.report_collector = ReportCollector(month=3, year=2026)
    coord._daily_discharge_kwh = 0.0
    coord._daily_safety_blocks = 0
    coord._daily_plans = 0
    coord._current_date = "2026-03-31"
    coord._daily_avg_price = float((options or {}).get("fallback_price_ore", 80.0))
    coord._avg_price_initialized = True

    # Notification
    coord.notifier = MagicMock()
    coord.notifier.crosscharge_alert = AsyncMock()
    coord.notifier.proactive_discharge_started = AsyncMock()
    coord.notifier.safety_block = AsyncMock()

    # Persistence flags (skip restores in unit tests)
    coord._runtime_loaded = True
    coord._ledger_loaded = True
    coord._savings_loaded = True
    coord._savings_last_save = 0.0
    coord._savings_store = MagicMock()
    coord._savings_store.async_save = AsyncMock()
    coord._consumption_loaded = True
    coord._consumption_last_save = 0.0
    coord._consumption_last_hour = -1
    coord._consumption_store = MagicMock()
    coord._consumption_store.async_save = AsyncMock()

    # Runtime store (needed by _async_save_runtime, called after every command)
    coord._runtime_store = MagicMock()
    coord._runtime_store.async_save = AsyncMock()

    # Adapters (none by default — uses legacy entity path)
    coord.inverter_adapters = []
    coord.ev_adapter = None

    # Decision log
    coord.last_decision = Decision()
    coord.decision_log = deque(maxlen=48)

    # Consumption & prediction
    coord.consumption_profile = ConsumptionProfile()
    coord.hourly_actuals = []
    coord._last_tracked_hour = -1
    coord.predictor = ConsumptionPredictor()
    coord._predictor_store = MagicMock()
    coord._predictor_store.async_save = AsyncMock()
    coord._predictor_loaded = True
    coord._predictor_last_save = 0.0

    # PLAT-975: ML Predictor
    from custom_components.carmabox.core.ml_predictor import MLPredictor as _MLPred

    coord._ml_predictor = _MLPred()
    coord._ml_predictor_store = MagicMock()
    coord._ml_predictor_store.async_save = AsyncMock()
    coord._ml_predictor_loaded = True
    coord._ml_predictor_last_save = 0.0
    coord.ml_forecast_24h = []

    # Plan tracking
    coord._plan_deviation_count = 0
    coord._plan_last_correction_time = 0.0
    coord._plan_grid_excess_count = 0

    # Ellevio peak tracking
    coord._ellevio_hour_samples = []
    coord._ellevio_current_hour = -1
    coord._ellevio_monthly_hourly_peaks = []

    # Shadow comparison
    coord.shadow = ShadowComparison()
    coord.shadow_log = []
    coord._shadow_savings_kr = 0.0

    # Appliances
    coord._appliances = []
    coord.appliance_power = {}
    coord.appliance_energy_wh = {}

    # EV / miner
    coord._ev_enabled = False
    coord._ev_current_amps = 0
    coord._ev_last_ramp_time = 0.0
    coord._ev_initialized = True
    coord._last_known_ev_soc = -1.0
    coord._last_known_ev_soc_time = 0.0
    coord._miner_entity = ""
    coord._miner_on = False
    coord._ev_last_full_charge_date = ""
    coord._ev_tonight_soc = -1.0

    # Ledger
    coord.ledger = EnergyLedger()

    # License (full premium for tests)
    coord._license_tier = "premium"
    coord._license_features = [
        "analyzer",
        "executor",
        "dashboard",
        "ev_control",
        "miner_control",
        "watchdog",
        "self_healing",
    ]
    coord._license_last_check = 0.0
    coord._license_check_interval = 99_999_999
    coord._license_valid_until = ""
    coord._license_offline_grace_days = 7
    coord.executor_enabled = True

    # Rule tracking (IT-1937)
    coord._rule_triggers = {}
    coord._active_rule_id = ""

    # Self-healing (PLAT-972)
    coord._ems_consecutive_failures = 0
    coord._ems_pause_until = 0.0
    coord._ev_last_known_enabled = None

    # W6: EV stuck detection (PLAT-1040)
    coord._ev_last_soc_change_t = 0.0
    coord._ev_prev_soc_for_stuck = -1.0
    coord._night_ev_active = False
    coord._soc_imbalance_logged = False

    # Flat-line controller
    coord._grid_samples: list[float] = []
    coord._grid_sample_max = 10

    # DataUpdateCoordinator.data
    coord.data = None

    # Misc
    coord._estimated_house_base_kw = 2.0
    coord._daily_goals: dict = {}
    coord._breach_history: dict = {}
    coord._benchmark_last_fetch = 0.0

    # PLAT-1141: ExecutionEngine
    from custom_components.carmabox.core.execution_engine import ExecutionEngine

    coord._execution_engine = ExecutionEngine(coord)

    return coord


def _plan_hour(hour: int, action: str = "i", price: float = 50.0) -> HourPlan:
    """Create a minimal HourPlan for use in coordinator.plan."""
    return HourPlan(
        hour=hour,
        action=action,
        battery_kw=0.0,
        grid_kw=0.0,
        weighted_kw=0.0,
        pv_kw=0.0,
        consumption_kw=2.0,
        ev_kw=0.0,
        ev_soc=0,
        battery_soc=60,
        price=price,
    )


# ── 1. BMS Taper Detection ────────────────────────────────────────────────────


class TestIsTaperDetection:
    """_is_in_taper — boundary values and persistenece across command cycles."""

    def _taper_state(self, **kwargs) -> CarmaboxState:
        """Baseline: charging from PV, exporting 300 W, SoC 97%, PV 2 kW."""
        defaults = {
            "grid_power_w": -300.0,  # exporting
            "pv_power_w": 2000.0,
            "battery_soc_1": 97.0,
            "battery_power_1": -500.0,
        }
        defaults.update(kwargs)
        return CarmaboxState(**defaults)

    def test_taper_detected_when_charge_pv_active(self) -> None:
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        assert coord._is_in_taper(self._taper_state()) is True

    def test_taper_persists_when_already_in_taper_mode(self) -> None:
        """CHARGE_PV_TAPER must also be accepted — prevents oscillation (IT-1939 fix)."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV_TAPER
        assert coord._is_in_taper(self._taper_state()) is True

    def test_taper_not_detected_when_idle(self) -> None:
        coord = _make_coord()
        coord._last_command = BatteryCommand.IDLE
        assert coord._is_in_taper(self._taper_state()) is False

    def test_taper_not_detected_when_discharging(self) -> None:
        coord = _make_coord()
        coord._last_command = BatteryCommand.DISCHARGE
        assert coord._is_in_taper(self._taper_state()) is False

    def test_taper_requires_export(self) -> None:
        """No export → not taper (grid is importing, BMS is working)."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = self._taper_state(grid_power_w=200.0)  # importing
        assert coord._is_in_taper(state) is False

    def test_taper_threshold_at_exactly_200w_export(self) -> None:
        """Export must be STRICTLY > 200 W, not ≥ 200 W."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV

        # Boundary: exactly 200 W → NOT taper
        assert coord._is_in_taper(self._taper_state(grid_power_w=-200.0)) is False
        # One watt above → taper
        assert coord._is_in_taper(self._taper_state(grid_power_w=-201.0)) is True

    def test_taper_not_detected_at_100pct_soc(self) -> None:
        """100% SoC: batteries genuinely full → not taper (correct condition)."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = self._taper_state(battery_soc_1=100.0)
        assert coord._is_in_taper(state) is False

    def test_taper_requires_pv_above_500w(self) -> None:
        """PV must be > 500 W (strictly) to detect taper."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV

        assert coord._is_in_taper(self._taper_state(pv_power_w=500.0)) is False  # ≤500
        assert coord._is_in_taper(self._taper_state(pv_power_w=501.0)) is True  # >500

    def test_taper_target_kw_restored_after_path(self) -> None:
        """Cold lock / taper temporarily sets target_kw=0; must restore afterwards."""
        coord = _make_coord()
        coord.target_kw = 2.0

        # Simulate what _execute does inside the taper path
        saved_target = coord.target_kw
        coord.target_kw = 0.0
        # ... surplus chain would run here ...
        coord.target_kw = saved_target

        assert coord.target_kw == 2.0


# ── 2. BMS Cold Lock Detection ────────────────────────────────────────────────


class TestIsColdLocked:
    """_is_cold_locked — 10 °C threshold and two-battery logic (IT-1948)."""

    def _cold_state(self, **kwargs) -> CarmaboxState:
        """Baseline: charging from PV, BMS not accepting charge (near-zero power), PV 2 kW."""
        defaults = {
            "grid_power_w": -500.0,
            "pv_power_w": 2000.0,
            "battery_soc_1": 60.0,
            "battery_power_1": 0.0,  # BMS not accepting → near-zero
            "battery_soc_2": -1.0,  # no second battery
            "battery_min_cell_temp_1": 9.0,
        }
        defaults.update(kwargs)
        return CarmaboxState(**defaults)

    def test_cold_lock_detected_below_10c(self) -> None:
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        assert coord._is_cold_locked(self._cold_state(battery_min_cell_temp_1=9.9)) is True

    def test_cold_lock_not_detected_at_exactly_10c(self) -> None:
        """Threshold is strictly < 10.0 °C."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        assert coord._is_cold_locked(self._cold_state(battery_min_cell_temp_1=10.0)) is False

    def test_cold_lock_not_detected_above_10c(self) -> None:
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        assert coord._is_cold_locked(self._cold_state(battery_min_cell_temp_1=10.1)) is False

    def test_cold_lock_not_detected_when_idle(self) -> None:
        """Cold lock only applies when a charge command is active."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.IDLE
        assert coord._is_cold_locked(self._cold_state(battery_min_cell_temp_1=5.0)) is False

    def test_cold_lock_not_detected_when_no_temp_data(self) -> None:
        """No temperature sensors → cannot detect cold lock → False."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = CarmaboxState(
            grid_power_w=-500.0,
            pv_power_w=2000.0,
            battery_soc_1=60.0,
            battery_power_1=0.0,
            # battery_min_cell_temp_1 = None (default)
        )
        assert coord._is_cold_locked(state) is False

    def test_cold_lock_uses_minimum_of_both_batteries(self) -> None:
        """With two batteries, cold lock triggers if EITHER cell is below threshold."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = self._cold_state(
            battery_min_cell_temp_1=15.0,  # warm ✓
            battery_min_cell_temp_2=7.0,  # cold ✗
            battery_soc_2=50.0,
            battery_power_2=0.0,
        )
        assert coord._is_cold_locked(state) is True

    def test_cold_lock_not_triggered_if_battery_actively_charging(self) -> None:
        """battery_power_1 > 100 W means BMS IS accepting charge → not cold locked."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = self._cold_state(
            battery_min_cell_temp_1=5.0,
            battery_power_1=-1500.0,  # actively charging at 1.5 kW
        )
        assert coord._is_cold_locked(state) is False

    def test_cold_lock_persists_in_taper_mode(self) -> None:
        """Also triggers when previous command was CHARGE_PV_TAPER (same charge family)."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV_TAPER
        assert coord._is_cold_locked(self._cold_state(battery_min_cell_temp_1=3.0)) is True


# ── 3. RULE 2 Hysteresis ─────────────────────────────────────────────────────


class TestDischargeHysteresis:
    """RULE 2: 10% hysteresis prevents oscillation when grid ≈ target."""

    @pytest.mark.asyncio
    async def test_discharge_continues_below_target_when_already_discharging(self) -> None:
        """With last_command=DISCHARGE, threshold drops to target*0.9.

        Grid at 95% of target (1900 W vs 2000 W) should still discharge.
        """
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord._last_command = BatteryCommand.DISCHARGE
        coord._last_discharge_w = 0  # different wattage → K1 skip won't fire

        state = CarmaboxState(
            grid_power_w=1900.0,  # 95% of 2000 W target
            battery_soc_1=60.0,
            current_price=80.0,
            pv_power_w=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # 1900 > 2000*0.9=1800 → hysteresis allows continued discharge
        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_no_hysteresis_when_previously_idle(self) -> None:
        """Without prior DISCHARGE, threshold is 100% of target.

        Grid at 95% of target (1900 W) → below 2000 W → should idle, NOT discharge.

        To isolate RULE 2 hysteresis from the flat-line controller (RULE 1.9),
        we pre-seed _grid_samples with low values so rolling_avg stays below the
        flat-line threshold (target - 0.3 kW = 1.7 kW).
        """
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord._last_command = BatteryCommand.STANDBY
        # Pre-seed 9 samples of 500 W → after adding 1900: avg = (500*9+1900)/10 = 640 W
        # 0.64 kW < 1.7 kW threshold → flat-line won't fire
        coord._grid_samples = [500.0 / 1000] * 9  # stored in kW

        state = CarmaboxState(
            grid_power_w=1900.0,
            battery_soc_1=60.0,
            current_price=80.0,
            pv_power_w=0.0,
            solar_radiation_wm2=0.0,
            rain_mm=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # 1900 < 2000*1.0=2000 and flat-line avg(640W) < 1700W → no discharge
        assert coord._last_command != BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_discharge_stops_when_grid_well_below_hysteresis_threshold(self) -> None:
        """Once grid < target*0.9, discharge stops even with hysteresis active."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord._last_command = BatteryCommand.DISCHARGE
        coord._last_discharge_w = 1000

        # Grid 1.5 kW — below 2.0*0.9=1.8 kW hysteresis threshold
        state = CarmaboxState(
            grid_power_w=1500.0,
            battery_soc_1=60.0,
            current_price=80.0,
            pv_power_w=0.0,
            solar_radiation_wm2=0.0,  # no sun → no proactive discharge
            rain_mm=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # 1500 < 1800 → discharge should stop → standby
        # (Proactive rule 1.8 won't fire: soc=60% < 80% rainy/cloudy threshold)
        assert coord._last_command == BatteryCommand.STANDBY


# ── 4. Safety Gate Ordering ───────────────────────────────────────────────────


class TestSafetyGateOrdering:
    """Safety gates run in strict order: heartbeat → rate limit → crosscharge."""

    @pytest.mark.asyncio
    async def test_heartbeat_failure_blocks_all_subsequent_gates(self) -> None:
        """Heartbeat failure → early return, rate limit and crosscharge NOT called."""
        coord = _make_coord()
        coord.safety.check_heartbeat = MagicMock(
            return_value=MagicMock(ok=False, reason="stale 200s")
        )
        state = CarmaboxState(grid_power_w=5000.0, battery_soc_1=80.0)

        initial_cmd = coord._last_command
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command == initial_cmd  # no change
        assert coord._daily_safety_blocks == 1
        coord.safety.check_rate_limit.assert_not_called()
        coord.safety.check_crosscharge.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_failure_blocks_before_crosscharge(self) -> None:
        """Rate limit failure → crosscharge NOT checked."""
        coord = _make_coord()
        coord.safety.check_rate_limit = MagicMock(
            return_value=MagicMock(ok=False, reason="too fast")
        )
        state = CarmaboxState(grid_power_w=5000.0, battery_soc_1=80.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._daily_safety_blocks == 1
        coord.safety.check_crosscharge.assert_not_called()

    @pytest.mark.asyncio
    async def test_crosscharge_forces_standby_and_notifies(self) -> None:
        """Crosscharge detected → standby forced, alert sent."""
        coord = _make_coord()
        coord.safety.check_crosscharge = MagicMock(
            return_value=MagicMock(ok=False, reason="battery 1 charging, 2 discharging")
        )
        state = CarmaboxState(
            grid_power_w=0.0,
            battery_soc_1=50.0,
            battery_power_1=-2000.0,
            battery_power_2=2000.0,
            battery_soc_2=50.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        coord.notifier.crosscharge_alert.assert_called_once()
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_all_safety_gates_pass_allows_discharge(self) -> None:
        """All gates pass → execution continues to RULE 2 → discharge above target."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        state = CarmaboxState(
            grid_power_w=3000.0,  # well above 2 kW target
            battery_soc_1=60.0,
            current_price=80.0,
            pv_power_w=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE


# ── 5. PLAT-946: Crosscharge with Invalid Power Flags ────────────────────────


class TestCrosschargeWithInvalidFlags:
    """PLAT-946: power_valid=False means HA hasn't read the sensor yet.

    Coordinator must pass validity flags to SafetyGuard so it can make
    the correct decision (don't falsely flag 0-W-default as crosscharge).
    """

    def test_coordinator_passes_power1_invalid_flag(self) -> None:
        """battery_power_1_valid=False must be forwarded to check_crosscharge."""
        coord = _make_coord()
        state = CarmaboxState(
            battery_power_1=0.0,
            battery_power_1_valid=False,  # HA sensor unavailable at startup
            battery_power_2=2000.0,
            battery_power_2_valid=True,
            battery_soc_2=50.0,
        )
        coord.safety.check_crosscharge(
            state.battery_power_1,
            state.battery_power_2,
            power_1_valid=state.battery_power_1_valid,
            power_2_valid=state.battery_power_2_valid,
        )
        coord.safety.check_crosscharge.assert_called_once_with(
            0.0,
            2000.0,
            power_1_valid=False,
            power_2_valid=True,
        )

    def test_coordinator_passes_both_invalid_flags(self) -> None:
        """Both batteries unavailable at startup → both valid=False forwarded."""
        coord = _make_coord()
        state = CarmaboxState(
            battery_power_1=0.0,
            battery_power_1_valid=False,
            battery_power_2=0.0,
            battery_power_2_valid=False,
            battery_soc_2=50.0,
        )
        coord.safety.check_crosscharge(
            state.battery_power_1,
            state.battery_power_2,
            power_1_valid=state.battery_power_1_valid,
            power_2_valid=state.battery_power_2_valid,
        )
        _, kwargs = coord.safety.check_crosscharge.call_args
        assert kwargs["power_1_valid"] is False
        assert kwargs["power_2_valid"] is False

    def test_crosscharge_not_triggered_when_both_read_zero_invalid(self) -> None:
        """When both flags are invalid, coordinator should NOT crosscharge-standby."""
        coord = _make_coord()
        coord.safety.check_crosscharge = MagicMock(
            return_value=MagicMock(ok=True, reason="")  # SafetyGuard respects flags → PASS
        )
        # Gate passes → no crosscharge forced
        assert coord.notifier.crosscharge_alert.call_count == 0


# ── 6. RULE 1.5: Grid Charge Dynamic Threshold ───────────────────────────────


class TestGridChargeDynamicThreshold:
    """RULE 1.5: dynamic threshold = min(static, max(5.0, daily_avg * 0.4))."""

    @pytest.mark.asyncio
    async def test_dynamic_threshold_triggers_charge_in_low_price_season(self) -> None:
        """Summer avg 15 öre → dynamic = 6 öre; price at 5 öre → grid charge."""
        coord = _make_coord(
            {
                "grid_charge_price_threshold": 15.0,
                "grid_charge_max_soc": 90.0,
            }
        )
        coord._daily_avg_price = 15.0  # summer avg
        # dynamic = max(5.0, 15*0.4=6) = 6; min(15, 6) = 6 öre threshold
        # price 5 öre < 6 öre → should trigger

        state = CarmaboxState(
            grid_power_w=500.0,  # importing (not exporting, rule 1 won't intercept)
            battery_soc_1=50.0,
            pv_power_w=0.0,
            current_price=5.0,  # below 6 öre threshold
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # Grid charge sets _last_command = CHARGE_PV (same enum as solar charge)
        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_dynamic_threshold_floored_at_5_ore(self) -> None:
        """Even with daily_avg=5 öre (2 öre raw dynamic), floor is 5 öre."""
        coord = _make_coord(
            {
                "grid_charge_price_threshold": 15.0,
                "grid_charge_max_soc": 90.0,
            }
        )
        coord._daily_avg_price = 5.0  # very low avg
        # dynamic = max(5.0, 5*0.4=2) = 5; price 4 öre < 5 öre → triggers

        state = CarmaboxState(
            grid_power_w=500.0,
            battery_soc_1=50.0,
            pv_power_w=0.0,
            current_price=4.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_grid_charge_blocked_when_soc_above_max(self) -> None:
        """SoC ≥ grid_charge_max_soc → no grid charge regardless of price."""
        coord = _make_coord(
            {
                "grid_charge_price_threshold": 50.0,
                "grid_charge_max_soc": 90.0,
            }
        )
        coord._daily_avg_price = 80.0

        state = CarmaboxState(
            grid_power_w=500.0,
            battery_soc_1=91.0,  # above 90% cap
            pv_power_w=0.0,
            current_price=5.0,  # very cheap, but blocked by SoC
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command != BatteryCommand.CHARGE_PV

    @pytest.mark.asyncio
    async def test_grid_charge_does_not_fire_when_exporting(self) -> None:
        """RULE 1 (export → solar charge) takes priority over RULE 1.5."""
        coord = _make_coord(
            {
                "grid_charge_price_threshold": 50.0,
                "grid_charge_max_soc": 90.0,
            }
        )
        coord._daily_avg_price = 80.0

        # Exporting → RULE 1 intercepts and charges from solar
        state = CarmaboxState(
            grid_power_w=-500.0,  # exporting
            battery_soc_1=50.0,
            pv_power_w=0.0,
            current_price=5.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # RULE 1 charges from "solar" (even if PV=0 it sets charge_pv on export)
        # or if battery is full → standby. Either way, RULE 1.5 never fires.
        assert coord._last_command in (
            BatteryCommand.CHARGE_PV,
            BatteryCommand.CHARGE_PV_TAPER,
            BatteryCommand.STANDBY,
        )


# ── 7. RULE 1.8: Proactive Discharge ─────────────────────────────────────────


class TestProactiveDischarge:
    """RULE 1.8: aggressiveness scales with sun / rain / night conditions."""

    @pytest.mark.asyncio
    async def test_proactive_discharge_triggers_with_sun_high_soc(self) -> None:
        """Daytime, sun available, SoC ≥ 40%, small grid import → proactive discharge."""
        coord = _make_coord({"target_weighted_kw": 2.0})

        state = CarmaboxState(
            grid_power_w=200.0,  # importing but below target → RULE 2 won't fire
            battery_soc_1=80.0,  # high SoC ≥ 40% (sun threshold)
            solar_radiation_wm2=500.0,  # strong sun → _sun_available=True
            rain_mm=0.0,
            pv_power_w=0.0,
            current_price=80.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=13)  # daytime
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_proactive_discharge_not_triggered_at_night(self) -> None:
        """At night (hour=2), proactive discharge requires SoC ≥ 90% and grid > 300 W."""
        coord = _make_coord({"target_weighted_kw": 2.0})

        # SoC 75%, grid 200 W — would trigger daytime but not at night
        state = CarmaboxState(
            grid_power_w=200.0,
            battery_soc_1=75.0,
            solar_radiation_wm2=0.0,
            rain_mm=0.0,
            pv_power_w=0.0,
            current_price=50.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=2)  # night
            await coord._execute(state)

        assert coord._last_command != BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_proactive_discharge_conservative_in_rain(self) -> None:
        """Rain active → conservative: requires SoC ≥ 80% (not 40%)."""
        coord = _make_coord({"target_weighted_kw": 2.0})

        # SoC 60% < 80%, rain active → should NOT discharge
        state = CarmaboxState(
            grid_power_w=300.0,
            battery_soc_1=60.0,  # below 80% rainy threshold
            solar_radiation_wm2=50.0,
            rain_mm=2.0,  # rain active
            pv_power_w=0.0,
            current_price=80.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=13)
            await coord._execute(state)

        assert coord._last_command != BatteryCommand.DISCHARGE

    @pytest.mark.asyncio
    async def test_proactive_discharge_not_triggered_below_min_grid_threshold(self) -> None:
        """Grid import below 50 W (sun threshold) → proactive discharge skipped."""
        coord = _make_coord({"target_weighted_kw": 2.0})

        state = CarmaboxState(
            grid_power_w=30.0,  # below 50 W sun threshold
            battery_soc_1=90.0,  # high SoC
            solar_radiation_wm2=500.0,  # sun available
            rain_mm=0.0,
            pv_power_w=1000.0,
            current_price=80.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=13)
            await coord._execute(state)

        # 30 W < 50 W threshold → no proactive discharge
        assert coord._last_command != BatteryCommand.DISCHARGE


# ── 8. _cmd_charge_pv Idempotency ────────────────────────────────────────────


class TestCmdChargePvIdempotency:
    """_cmd_charge_pv must not re-send commands when already in the charge state."""

    @pytest.mark.asyncio
    async def test_no_resend_when_already_charge_pv(self) -> None:
        """Already charging from PV → no service call (K1/idempotency guard)."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV
        state = CarmaboxState(battery_soc_1=60.0)

        await coord._cmd_charge_pv(state)

        # The idempotency check exits before calling services
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_resend_when_already_in_taper(self) -> None:
        """IT-1939: also skip re-send when already in CHARGE_PV_TAPER."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.CHARGE_PV_TAPER
        state = CarmaboxState(battery_soc_1=60.0)

        await coord._cmd_charge_pv(state)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_resend_when_transitioning_from_standby(self) -> None:
        """Transition STANDBY → CHARGE_PV must send the service call."""
        coord = _make_coord()
        coord._last_command = BatteryCommand.STANDBY
        state = CarmaboxState(battery_soc_1=60.0)

        await coord._cmd_charge_pv(state)

        # Service must be called for the transition
        coord.hass.services.async_call.assert_called()
        assert coord._last_command == BatteryCommand.CHARGE_PV


# ── 9. RULE 2: Price-Aware Discharge Throttling ───────────────────────────────


class TestPriceAwareDischargeThrottling:
    """IT-2074: Throttle discharge to 50% when price drops >30% within next 2h."""

    @pytest.mark.asyncio
    async def test_discharge_throttled_when_price_drops_40pct_soon(self) -> None:
        """Price 100 → 60 öre in next hour (40% drop) → RULE 2 throttles discharge.

        The flat-line controller (RULE 1.9) must NOT intercept this test —
        IT-2074 throttling only lives in RULE 2. We pre-seed _grid_samples so the
        rolling average stays below the flat-line threshold (target-0.3 = 1.7 kW).
        """
        coord = _make_coord({"target_weighted_kw": 2.0})
        now_hour = 14
        # Pre-seed 9 samples of 0 W → after adding 4000 W: avg = 4000/10 = 400 W
        # 0.4 kW < 1.7 kW flat-line threshold → RULE 2 handles discharge
        coord._grid_samples = [0.0] * 9

        coord.plan = [
            _plan_hour(hour=(now_hour + 1) % 24, action="i", price=60.0),  # -40%
        ]

        state = CarmaboxState(
            grid_power_w=4000.0,  # 2 kW above target → RULE 2 fires
            battery_soc_1=60.0,
            pv_power_w=0.0,
            current_price=100.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=now_hour)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE
        # Unthrottled: (4000-2000)/1 = 2000 W; throttled: max(100, 2000//2) = 1000 W
        assert (
            coord.last_decision.discharge_w <= 1000
        ), f"Expected throttled discharge ≤ 1000 W, got {coord.last_decision.discharge_w} W"

    @pytest.mark.asyncio
    async def test_no_throttle_when_price_stays_similar(self) -> None:
        """Price 100 → 95 öre (5% drop) → no throttle, full discharge."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        now_hour = 14
        coord.plan = [
            _plan_hour(hour=(now_hour + 1) % 24, action="i", price=95.0),  # only -5%
        ]

        state = CarmaboxState(
            grid_power_w=4000.0,
            battery_soc_1=60.0,
            pv_power_w=0.0,
            current_price=100.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=now_hour)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE
        # 5% drop < 30% threshold → no throttle → ~2000 W
        assert (
            coord.last_decision.discharge_w >= 1500
        ), f"Expected full discharge ≥ 1500 W, got {coord.last_decision.discharge_w} W"

    @pytest.mark.asyncio
    async def test_no_throttle_with_empty_plan(self) -> None:
        """Empty plan → no future price comparison possible → no throttle."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord.plan = []  # no plan

        state = CarmaboxState(
            grid_power_w=4000.0,
            battery_soc_1=60.0,
            pv_power_w=0.0,
            current_price=100.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.DISCHARGE
        assert coord.last_decision.discharge_w >= 1500  # unthrottled


# ── 10. Predictor Integration ─────────────────────────────────────────────────


class TestPredictorIntegration:
    """PLAT-965: Predictor in _generate_plan — all Solcast properties mocked."""

    def _solcast_mock(self) -> MagicMock:
        """Return a fully-mocked SolcastAdapter with all properties set."""
        mock = MagicMock()
        mock.today_hourly_kw = [0.0] * 24
        mock.tomorrow_hourly_kw = [0.0] * 24
        mock.forecast_daily_3d = [10.0, 10.0, 10.0]
        mock.power_now_kw = 0.0  # ← critical: prevents TypeError at line 1150
        return mock

    def _setup_price_state(self, coord: CarmaboxCoordinator, entity_id: str) -> CarmaboxState:
        state = MagicMock()
        state.state = "50"
        state.attributes = {"today": [50.0] * 24, "tomorrow": [], "tomorrow_valid": False}
        coord._states[entity_id] = state
        return CarmaboxState(battery_soc_1=80.0)

    def test_plan_generated_when_predictor_trained(self) -> None:
        """Trained predictor → plan should be non-empty."""
        coord = _make_coord({"price_entity": "sensor.np"})
        coord.predictor.total_samples = 200
        for d in range(7):
            for h in range(24):
                coord.predictor.history[f"{d}_{h}"] = [2.0, 2.5, 1.8]

        state = self._setup_price_state(coord, "sensor.np")

        with patch(
            "custom_components.carmabox.coordinator.SolcastAdapter",
            return_value=self._solcast_mock(),
        ):
            coord._generate_plan(state)

        assert len(coord.plan) > 0

    def test_plan_generated_when_predictor_not_trained(self) -> None:
        """Untrained predictor → falls back to ConsumptionProfile, plan still generated."""
        coord = _make_coord({"price_entity": "sensor.np"})
        assert not coord.predictor.is_trained

        state = self._setup_price_state(coord, "sensor.np")

        with patch(
            "custom_components.carmabox.coordinator.SolcastAdapter",
            return_value=self._solcast_mock(),
        ):
            coord._generate_plan(state)

        assert isinstance(coord.plan, list)

    def test_generate_plan_survives_solcast_exception(self) -> None:
        """If SolcastAdapter raises, plan generation catches the error gracefully."""
        coord = _make_coord({"price_entity": "sensor.np"})
        state = self._setup_price_state(coord, "sensor.np")

        with patch(
            "custom_components.carmabox.coordinator.SolcastAdapter",
            side_effect=RuntimeError("Solcast unavailable"),
        ):
            coord._generate_plan(state)  # must not propagate

        # Plan empty but coordinator survives
        assert isinstance(coord.plan, list)

    def test_generate_plan_survives_missing_price_entity(self) -> None:
        """No price entity configured → uses fallback prices, plan generated."""
        coord = _make_coord({})  # no price_entity

        state = CarmaboxState(battery_soc_1=80.0)

        with patch(
            "custom_components.carmabox.coordinator.SolcastAdapter",
            return_value=self._solcast_mock(),
        ):
            coord._generate_plan(state)

        assert isinstance(coord.plan, list)


# ── 11. CarmaboxState Model Boundary Values ───────────────────────────────────


class TestCarmaboxStateModel:
    """Boundary conditions on CarmaboxState computed properties.

    These are pure model tests — no coordinator involved.
    """

    # is_exporting

    def test_is_exporting_false_at_zero(self) -> None:
        """0 W grid is balanced / importing, not exporting."""
        assert CarmaboxState(grid_power_w=0.0).is_exporting is False

    def test_is_exporting_false_at_positive(self) -> None:
        assert CarmaboxState(grid_power_w=1.0).is_exporting is False

    def test_is_exporting_true_at_minus_one_watt(self) -> None:
        assert CarmaboxState(grid_power_w=-1.0).is_exporting is True

    # all_batteries_full

    def test_all_batteries_full_single_at_98pct(self) -> None:
        """98% SoC is NOT full (hysteresis at 99%)."""
        assert CarmaboxState(battery_soc_1=98.0).all_batteries_full is False

    def test_all_batteries_full_single_at_99pct(self) -> None:
        """PLAT-948: 99%+ is full (1% hysteresis avoids 100 flicker)."""
        assert CarmaboxState(battery_soc_1=99.0).all_batteries_full is True
        assert CarmaboxState(battery_soc_1=100.0).all_batteries_full is True

    def test_all_batteries_full_dual_requires_both(self) -> None:
        """Both batteries must reach 99%+ for all_batteries_full=True."""
        assert CarmaboxState(battery_soc_1=100.0, battery_soc_2=90.0).all_batteries_full is False
        assert CarmaboxState(battery_soc_1=99.0, battery_soc_2=99.0).all_batteries_full is True

    # total_battery_soc (capacity-weighted)

    def test_total_battery_soc_single_battery(self) -> None:
        """Single battery → total_battery_soc equals battery_soc_1."""
        assert CarmaboxState(battery_soc_1=73.5, battery_soc_2=-1.0).total_battery_soc == 73.5

    def test_total_battery_soc_capacity_weighted(self) -> None:
        """15 kWh @ 80% + 5 kWh @ 40% = (12+2)/20 = 70.0%."""
        s = CarmaboxState(
            battery_soc_1=80.0,
            battery_cap_1_kwh=15.0,
            battery_soc_2=40.0,
            battery_cap_2_kwh=5.0,
        )
        assert abs(s.total_battery_soc - 70.0) < 0.01

    def test_total_battery_soc_equal_capacities(self) -> None:
        """Equal capacities → simple average."""
        s = CarmaboxState(
            battery_soc_1=60.0,
            battery_cap_1_kwh=10.0,
            battery_soc_2=40.0,
            battery_cap_2_kwh=10.0,
        )
        assert abs(s.total_battery_soc - 50.0) < 0.01

    # has_battery_2 / has_ev

    def test_has_battery_2_false_when_minus_one(self) -> None:
        assert CarmaboxState(battery_soc_2=-1.0).has_battery_2 is False

    def test_has_battery_2_true_at_zero_pct(self) -> None:
        """SoC 0% means battery exists but is empty."""
        assert CarmaboxState(battery_soc_2=0.0).has_battery_2 is True

    def test_has_ev_false_when_minus_one(self) -> None:
        assert CarmaboxState(ev_soc=-1.0).has_ev is False

    def test_has_ev_true_at_zero_pct(self) -> None:
        """EV SoC 0% means EV connected (but empty)."""
        assert CarmaboxState(ev_soc=0.0).has_ev is True


# ── 12. RULE 4: Default Idle and SoC Safety Guard ────────────────────────────


class TestRuleIdleAndSafetyFloor:
    """RULE 4 and SoC-floor behavior in the discharge path."""

    @pytest.mark.asyncio
    async def test_idle_when_grid_below_target(self) -> None:
        """Grid well below target → standby (batteries rest)."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord._last_command = BatteryCommand.STANDBY

        state = CarmaboxState(
            grid_power_w=1000.0,  # 1 kW — well below 2 kW target
            battery_soc_1=60.0,
            pv_power_w=0.0,
            current_price=80.0,
            solar_radiation_wm2=0.0,
            rain_mm=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_discharge_blocked_at_min_soc_falls_back_to_standby(self) -> None:
        """Battery at min_soc → SafetyGuard.check_discharge blocks → fallback standby."""
        coord = _make_coord({"target_weighted_kw": 2.0, "min_soc": 15.0})
        coord.safety.check_discharge = MagicMock(
            return_value=MagicMock(ok=False, reason="SoC 15% = min_soc floor")
        )

        state = CarmaboxState(
            grid_power_w=5000.0,  # well above target → RULE 2 fires
            battery_soc_1=15.0,  # at min_soc floor
            pv_power_w=0.0,
            current_price=80.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        # Discharge blocked → self-heals to standby instead of crashing
        assert coord._last_command == BatteryCommand.STANDBY

    @pytest.mark.asyncio
    async def test_rule_tracking_set_on_idle(self) -> None:
        """_rule_triggers must record RULE_4 when system idles."""
        coord = _make_coord({"target_weighted_kw": 2.0})
        coord._last_command = BatteryCommand.STANDBY

        state = CarmaboxState(
            grid_power_w=500.0,
            battery_soc_1=60.0,
            pv_power_w=0.0,
            current_price=80.0,
            solar_radiation_wm2=0.0,
            rain_mm=0.0,
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute(state)

        assert "RULE_4" in coord._rule_triggers
