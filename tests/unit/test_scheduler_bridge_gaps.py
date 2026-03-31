"""Coverage tests — batch 18.

Targets:
  optimizer/scheduler.py:  208, 214-215, 234, 244, 261, 264-270, 327,
                            431, 461-466, 471-475, 723, 732, 801, 835,
                            1128, 1296
  coordinator_bridge.py:   444, 540, 545, 612, 639, 724-725, 888-890,
                            916-917, 1019-1021, 1051-1059
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

# ══════════════════════════════════════════════════════════════════════════════
# optimizer/scheduler.py — _schedule_ev_backwards
# ══════════════════════════════════════════════════════════════════════════════


class TestScheduleEvBackwards:
    """Lines 208, 214-215, 234, 244, 261, 264-270."""

    def test_high_load_forces_zero_headroom_and_third_pass(self) -> None:
        """Very high house load → grid_headroom=0 → desired_kw=0 (line 208),
        amps=0/charge_kw=0 (lines 214-215), then pass3 forces min amps (lines 261-270)."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_ev_backwards

        # Night hours starting at 22, all 8 slots night hours.
        # High load → no headroom for EV → all charge_kw=0 → pass3 forces min amps.
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=20.0,  # Needs substantial charging
            ev_capacity_kwh=77.0,
            morning_target_soc=75.0,
            hourly_prices=[50.0] * 8,
            hourly_loads=[4.0] * 8,  # High load → effective_load=4.0
            target_weighted_kw=1.5,  # Low target → headroom=(1.5*0.85/0.5 - 4.0)=-1.45 → 0
            battery_kwh_available=0.01,  # Near-zero battery support
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=50.0,
            learnings=[],
        )
        # Should still produce a schedule (forced min-amps in pass3)
        assert len(result) == 8
        # At least some hours should have EV charging (pass3 forces it)
        assert any(kw > 0 for kw, _ in result)

    def test_pass1_break_enough_cheap_slots(self) -> None:
        """Pass 1 finds enough energy → break at line 234."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_ev_backwards

        # Low EV need + large slot capacity → pass1 fills in 1 slot → break on slot 2
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=40.0,
            ev_capacity_kwh=5.0,  # Small EV → energy_needed ≈ 1.65 kWh
            morning_target_soc=60.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[0.5] * 8,  # Low load → plenty of headroom
            target_weighted_kw=4.0,  # High target → max headroom
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=10.0,
            daily_consumption_kwh=10.0,
            learnings=[],
        )
        assert len(result) == 8

    def test_pass2_break_appliance_window_covers_need(self) -> None:
        """All hours avoided → pass1 skips all → pass2 first slot covers need → break (line 244)."""
        from custom_components.carmabox.optimizer.models import BreachLearning
        from custom_components.carmabox.optimizer.scheduler import _schedule_ev_backwards

        # Mark all night hours as avoided via learnings → pass1 skips → pass2 processes them
        # Small EV need (0.65 kWh) → first pass2 slot (eff_kwh≈5kWh) covers it
        # → second iteration checks remaining<=0.1 → break line 244
        avoid_hours = [22, 23, 0, 1, 2, 3, 4, 5]
        learnings = [
            BreachLearning(
                pattern=f"test_{h}",
                hour=h,
                description="test",
                action="pause_ev",
                confidence=0.9,
                occurrences=3,
            )
            for h in avoid_hours
        ]
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=30.0,
            ev_capacity_kwh=5.0,  # energy_needed=(40-30*0.9)/100*5=0.65 kWh
            morning_target_soc=40.0,
            hourly_prices=[30.0] * 8,
            hourly_loads=[1.0] * 8,  # Low load → charge_kw>0
            target_weighted_kw=4.0,
            battery_kwh_available=10.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=10.0,
            learnings=learnings,
        )
        assert len(result) == 8

    def test_pass3_break_energy_covered_in_one_slot(self) -> None:
        """All charge_kw=0 → pass3 forces min_kw → first slot covers small need → break (261)."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_ev_backwards

        # All slots have zero headroom → charge_kw=0, pass1/pass2 skip all.
        # energy_needed = (40-30*0.9)/100*5 = 0.65 kWh.
        # Pass3 slot 0: forced min_kw=1.38, eff_kwh=1.24 → remaining=0.65-1.24=-0.59 ≤ 0.1
        # Pass3 slot 1: remaining≤0.1 → break (line 261) ✓
        result = _schedule_ev_backwards(
            num_hours=8,
            start_hour=22,
            ev_soc_pct=30.0,
            ev_capacity_kwh=5.0,
            morning_target_soc=40.0,  # energy_needed = 0.65 kWh
            hourly_prices=[50.0] * 8,
            hourly_loads=[4.0] * 8,  # High load → headroom=max(0,1.5*0.85/0.5-4)=0
            target_weighted_kw=1.5,  # Very tight → charge_kw=0 on all slots
            battery_kwh_available=0.0,
            pv_tomorrow_kwh=0.0,
            daily_consumption_kwh=50.0,
            learnings=[],
        )
        assert len(result) == 8
        assert any(kw > 0 for kw, _ in result)  # pass3 forced some charging


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/scheduler.py — _schedule_battery
# ══════════════════════════════════════════════════════════════════════════════


class TestScheduleBattery:
    """Lines 327, 431, 461-466, 471-475."""

    def test_pv_forecast_reserve_multi_day(self) -> None:
        """pv_forecast_daily[1] - consumption > 10 → break in reserve loop (line 327)."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_battery

        # Small loads → daily_consumption=12.0 kWh. pv_day=25 → surplus=13>10 → break (327)
        result = _schedule_battery(
            num_hours=8,
            start_hour=0,
            hourly_prices=[50.0] * 8,
            hourly_pv=[0.0] * 8,
            hourly_loads=[0.5] * 8,  # sum=4.0 / 8 * 24 = 12.0 kWh/day
            hourly_ev=[0.0] * 8,
            target_weighted_kw=3.0,
            battery_soc_pct=50.0,
            battery_cap_kwh=10.0,
            pv_forecast_daily=[5.0, 25.0],  # pv_day=25, surplus=25-12=13>10 → break
        )
        assert len(result) == 8

    def test_aggressive_discharge_high_price(self) -> None:
        """Low median + high-spike price → aggressive_threshold=60 exceeded → line 431.

        prices=[30]*6+[250]*2: median=30, discharge_thr=40, aggressive_thr=60.
        soc=50%: drain_budget=0 (no P5). price=30<40 → idle. price=250>=60 → aggressive.
        """
        from custom_components.carmabox.optimizer.scheduler import _schedule_battery

        result = _schedule_battery(
            num_hours=8,
            start_hour=8,  # Day hours w=1.0
            hourly_prices=[30.0] * 6 + [250.0] * 2,
            hourly_pv=[0.0] * 8,
            hourly_loads=[2.0] * 8,  # net=2.0 ≤ 4.0*0.85=3.4 → P3 no-fire
            hourly_ev=[0.0] * 8,
            target_weighted_kw=4.0,
            battery_soc_pct=50.0,  # drain_budget=0 → P5 no-fire; available=3.2>0.3 ✓
            battery_cap_kwh=10.0,
        )
        assert any(action == "d" for _, action in result)

    def test_ev_support_discharge_medium_price(self) -> None:
        """EV charging + medium price + load below Ellevio limit → P6 EV support (lines 461-466).

        target=4.0: net*w=6.3*0.5=3.15 ≤ 4.0*0.85=3.4 → P3 not fire.
        price=20 < threshold=40 → P4 not fire. drain_budget=0 → P5 not fire.
        ev=3>0, w=0.5>0 → P6 fires. total_load=3.15>target*0.7=2.8 → lines 461-466."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_battery

        result = _schedule_battery(
            num_hours=8,
            start_hour=22,  # Night: w=0.5
            hourly_prices=[20.0] * 8,  # >15 (no grid charge), <40 (no arbitrage)
            hourly_pv=[0.0] * 8,
            hourly_loads=[3.3] * 8,  # net=3.3+3.0=6.3, net*w=3.15 ≤ 4.0*0.85=3.4
            hourly_ev=[3.0] * 8,  # EV → P6 candidate
            target_weighted_kw=4.0,  # High enough that P3 doesn't fire
            battery_soc_pct=50.0,  # drain_budget=max(0,5-5)=0 → P5 not fire
            battery_cap_kwh=10.0,
        )
        assert any(action == "d" for _, action in result)

    def test_anti_idle_discharge_high_soc(self) -> None:
        """High SoC + no charge triggers + no EV → anti-idle fires (lines 471-475)."""
        from custom_components.carmabox.optimizer.scheduler import _schedule_battery

        # Day hours, PV>1kW at slot 0 → sunrise_slot=0 → before_sunrise=False always.
        # Battery at 85% (>80%), medium load, price not triggering charge/discharge.
        result = _schedule_battery(
            num_hours=8,
            start_hour=8,  # Day hours, w=1.0
            hourly_prices=[20.0] * 8,  # >15 no grid-charge, <40 no arbitrage
            hourly_pv=[2.0] * 8,  # PV>1 at slot0 → sunrise_slot=0 → before_sunrise=False
            hourly_loads=[3.5] * 8,  # net=3.5-2.0=1.5, net*1.0=1.5 ≤ 2.0*0.85=1.7 → not p3
            hourly_ev=[0.0] * 8,
            target_weighted_kw=2.0,
            battery_soc_pct=85.0,  # >80% → anti-idle triggers
            battery_cap_kwh=10.0,
        )
        # Should discharge some (anti-idle)
        assert any(action == "d" for _, action in result)


# ══════════════════════════════════════════════════════════════════════════════
# optimizer/scheduler.py — update_learnings, plan_ev_full_charge,
#                           _apply_corrections, generate_scheduler_plan, _pad
# ══════════════════════════════════════════════════════════════════════════════


class TestSchedulerMiscGaps:
    """Lines 723, 732, 801, 835, 1128, 1296."""

    def test_update_learnings_miner_load(self) -> None:
        """Load with 'miner' → parts.append('miner') (line 723)."""
        from custom_components.carmabox.optimizer.models import BreachRecord
        from custom_components.carmabox.optimizer.scheduler import update_learnings

        breach = BreachRecord(
            timestamp="2026-01-01T10:00:00",
            hour=10,
            actual_weighted_kw=2.5,
            target_kw=2.0,
            loads_active=["miner:500W", "house:1.5kW"],  # Contains "miner"
            root_cause="Miner aktiv",
            remediation="Stäng av miner",
            severity="minor",
        )
        result = update_learnings([], breach)
        assert len(result) == 1
        assert "miner" in result[0].pattern

    def test_update_learnings_ev_reduce_amps(self) -> None:
        """root_cause has 'EV' but not 'vitvaror' → reduce_ev_amps (line 732)."""
        from custom_components.carmabox.optimizer.models import BreachRecord
        from custom_components.carmabox.optimizer.scheduler import update_learnings

        breach = BreachRecord(
            timestamp="2026-01-01T17:00:00",
            hour=17,
            actual_weighted_kw=3.0,
            target_kw=2.0,
            loads_active=["EV:8A"],
            root_cause="EV laddar på kvällen",  # EV but no vitvaror
            remediation="Minska EV-effekt",
            severity="major",
        )
        result = update_learnings([], breach)
        assert len(result) == 1
        assert result[0].action == "reduce_ev_amps"

    def test_plan_ev_full_charge_saturday_adds_7(self) -> None:
        """current_weekday=5 (Saturday) → days_to_saturday=0 → set to 7 (line 801)."""
        from custom_components.carmabox.optimizer.scheduler import plan_ev_full_charge

        # No sunny days → falls through to fallback; today=Saturday → +7
        result = plan_ev_full_charge(
            days_since_full=8,  # > 5 (INTERVAL-2) → proceed
            pv_forecast_daily=[0.0] * 7,  # No sun → never returns early
            current_weekday=5,  # Saturday
        )
        assert result != ""  # Should return next Saturday

    def test_apply_corrections_target_hour_out_of_range(self) -> None:
        """target_hour maps to idx >= num_hours → continue (line 835)."""
        from custom_components.carmabox.optimizer.models import BreachCorrection
        from custom_components.carmabox.optimizer.scheduler import _apply_corrections

        # target_hour=20, start_hour=8, num_hours=8: idx=(20-8)%24=12 >= 8 → skip
        corr = BreachCorrection(
            created="2026-01-01T08:00:00",
            source_breach_hour=20,
            action="reduce_ev",
            target_hour=20,  # idx = (20-8)%24 = 12 >= num_hours=8 → continue
            param="",
            reason="Test",
        )
        ev_schedule = [(0.0, 0)] * 8
        battery_schedule: list[tuple[float, str]] = [(0.0, "i")] * 8
        result = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev_schedule,
            battery_schedule=battery_schedule,
            start_hour=8,
            num_hours=8,
            battery_soc_pct=50.0,
            battery_min_soc=20.0,
            battery_cap_kwh=10.0,
            max_discharge_kw=5.0,
        )
        assert result is not None

    def test_generate_scheduler_plan_grid_charge_reason(self) -> None:
        """Very cheap prices → batt_action='g' → Nät-laddning reason (line 1128)."""
        from custom_components.carmabox.optimizer.scheduler import generate_scheduler_plan

        plan = generate_scheduler_plan(
            start_hour=0,
            num_hours=24,
            hourly_prices=[5.0] * 24,  # Extremely cheap → grid charge
            battery_soc_pct=20.0,  # Low SoC → will charge
            battery_cap_kwh=10.0,
            grid_charge_price_threshold=15.0,
        )
        # Should have at least one grid charge slot
        assert any(slot.action == "g" for slot in plan.slots)

    def test_pad_long_list_truncates(self) -> None:
        """len(lst) >= n → return lst[:n] (line 1295)."""
        from custom_components.carmabox.optimizer.scheduler import _pad

        result = _pad([1.0, 2.0, 3.0, 4.0, 5.0], n=3, default=0.0)
        assert result == [1.0, 2.0, 3.0]
        assert len(result) == 3

    def test_pad_short_list_pads(self) -> None:
        """len(lst) < n → return lst + [default]*(n-len) (line 1296)."""
        from custom_components.carmabox.optimizer.scheduler import _pad

        result = _pad([1.0, 2.0], n=5, default=9.9)
        assert result == [1.0, 2.0, 9.9, 9.9, 9.9]

    def test_apply_corrections_shift_ev_param_type_error(self) -> None:
        """shift_ev param.split raises TypeError → except params={} (lines 857-858)."""
        from custom_components.carmabox.optimizer.models import BreachCorrection
        from custom_components.carmabox.optimizer.scheduler import _apply_corrections

        class _BadStr(str):
            def split(self, *a: object, **kw: object) -> list:  # type: ignore[override]
                raise TypeError("forced")

        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=0,
            action="shift_ev",
            target_hour=0,
            param="",
            reason="test",
        )
        corr.param = _BadStr("")  # type: ignore[assignment]
        ev_schedule = [(2.0, 8)] * 8
        battery_schedule: list[tuple[float, str]] = [(0.0, "i")] * 8
        result = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev_schedule,
            battery_schedule=battery_schedule,
            start_hour=0,
            num_hours=8,
            battery_soc_pct=50.0,
            battery_min_soc=15.0,
            battery_cap_kwh=10.0,
            max_discharge_kw=5.0,
        )
        assert result is not None  # except clause executed; no crash

    def test_apply_corrections_add_discharge_param_type_error(self) -> None:
        """add_discharge param.split raises TypeError → except params={} (lines 878-879)."""
        from custom_components.carmabox.optimizer.models import BreachCorrection
        from custom_components.carmabox.optimizer.scheduler import _apply_corrections

        class _BadStr(str):
            def split(self, *a: object, **kw: object) -> list:  # type: ignore[override]
                raise TypeError("forced")

        corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=0,
            action="add_discharge",
            target_hour=2,  # idx=2 < 8 → enters elif add_discharge branch
            param="",
            reason="test",
        )
        corr.param = _BadStr("")  # type: ignore[assignment]
        ev_schedule = [(0.0, 0)] * 8
        battery_schedule: list[tuple[float, str]] = [(0.0, "i")] * 8
        result = _apply_corrections(
            corrections=[corr],
            ev_schedule=ev_schedule,
            battery_schedule=battery_schedule,
            start_hour=0,
            num_hours=8,
            battery_soc_pct=80.0,  # avail_kwh = (80-15)/100*10 = 6.5 > 1.0 → discharge set
            battery_min_soc=15.0,
            battery_cap_kwh=10.0,
            max_discharge_kw=5.0,
        )
        assert result is not None

    def test_analyze_idle_time_high_idle_pct(self) -> None:
        """idle_pct > 70 → 'idle >70%' opportunity appended (line 1273)."""
        from custom_components.carmabox.optimizer.models import SchedulerHourSlot
        from custom_components.carmabox.optimizer.scheduler import analyze_idle_time

        # All slots idle (action='i') so active_slots=0
        slots = [
            SchedulerHourSlot(
                hour=h,
                action="i",
                battery_kw=0.0,
                ev_kw=0.0,
                ev_amps=0,
                miner_on=False,
                grid_kw=1.5,
                weighted_kw=1.5,
                pv_kw=0.0,
                consumption_kw=1.5,
                price=50.0,
                battery_soc=50,
                ev_soc=60,
                constraint_ok=True,
                reasoning="idle",
            )
            for h in range(24)
        ]
        # idle_minutes_today=1440 → 1440/(hour*60)*100 always clamps to 100 > 70
        result = analyze_idle_time(
            slots=slots,
            idle_minutes_today=1440,  # max minutes/day → idle_pct clamps to 100 always > 70
            battery_soc_pct=50.0,
            battery_min_soc=15.0,
            battery_cap_kwh=10.0,
            prices=[50.0] * 24,
            pv_forecast=[0.0] * 24,
        )
        assert any(">70%" in opp for opp in result.opportunities)


# ══════════════════════════════════════════════════════════════════════════════
# coordinator_bridge.py
# ══════════════════════════════════════════════════════════════════════════════


def _make_bridge() -> object:
    """Build CoordinatorBridge bypassing HA init."""
    from custom_components.carmabox.coordinator import BatteryCommand
    from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

    bridge = object.__new__(CoordinatorBridge)
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    bridge.hass = hass
    bridge._cfg = {}
    bridge._state_restored = True
    bridge._startup_safety_confirmed = True
    bridge._consecutive_errors = 0
    bridge.inverter_adapters = []
    bridge.ev_adapter = None
    bridge.executor_enabled = False
    bridge._miner_entity = ""
    bridge._last_plan_time = time.monotonic()  # Recent → no plan trigger
    bridge._last_save_time = 0.0
    bridge._use_v2 = False
    bridge.plan = []
    bridge.data = None
    bridge._breach_load_shed_active = False
    bridge.target_kw = 4.0
    bridge.night_ev_active = False
    bridge._last_command = BatteryCommand.STANDBY
    bridge._ev_enabled = False
    bridge._ev_current_amps = 6
    bridge._ellevio_hour_samples = []
    bridge._ellevio_current_hour = 0
    return bridge


class TestCoordinatorBridgeBatch18:
    """Lines 444, 540, 545, 612, 639, 724-725, 888-890, 916-917, 1019-1021, 1051-1059."""

    # ── fast_charging error path ────────────────────────────────────────────

    def test_execute_battery_commands_fast_charging_fail(self) -> None:
        """GoodWe adapter + fast_charging change + set_fast_charging fails → error (line 444)."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge()
        mock_adapter = MagicMock(spec=GoodWeAdapter)
        mock_adapter.set_ems_mode = AsyncMock(return_value=True)
        mock_adapter.set_discharge_limit = AsyncMock(return_value=True)
        mock_adapter.fast_charging_on = False  # Current state: OFF
        mock_adapter.set_fast_charging = AsyncMock(return_value=False)  # Fails
        bridge.inverter_adapters = [mock_adapter]  # type: ignore[union-attr]

        # Request fast_charging=True while current is False → mismatch → set_fast_charging called
        commands = [{"id": 0, "mode": "charge_pv", "power_limit": 0, "fast_charging": True}]
        asyncio.get_event_loop().run_until_complete(
            bridge._execute_battery_commands(commands)  # type: ignore[union-attr]
        )
        mock_adapter.set_fast_charging.assert_called_once_with(on=True, authorized=True)

    # ── crosscharge fix error paths ─────────────────────────────────────────

    def test_detect_and_fix_crosscharge_adapter_failures(self) -> None:
        """Crosscharge: set_ems_mode/set_discharge_limit fail → errors (lines 540, 545)."""
        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter
        from custom_components.carmabox.core.coordinator_v2 import SystemState

        bridge = _make_bridge()
        mock_a1 = MagicMock(spec=GoodWeAdapter)
        mock_a1.set_ems_mode = AsyncMock(return_value=False)  # Fails → line 540
        mock_a1.set_discharge_limit = AsyncMock(return_value=False)  # Fails → line 545
        bridge.inverter_adapters = [mock_a1, mock_a1]  # type: ignore[union-attr]

        sys_state = SystemState(
            battery_power_1=500.0,  # bat1 charging
            battery_power_2=-500.0,  # bat2 discharging → crosscharge!
        )
        asyncio.get_event_loop().run_until_complete(
            bridge._detect_and_fix_crosscharge(sys_state)  # type: ignore[union-attr]
        )
        mock_a1.set_ems_mode.assert_called()

    # ── _generate_plan paths ────────────────────────────────────────────────

    def test_generate_plan_pads_short_pv_and_zero_battery_cap(self) -> None:
        """Short PV → padding loop (line 612). zero battery → weighted=bat_soc_1 (line 639)."""
        import asyncio as _asyncio

        bridge = _make_bridge()
        bridge._last_plan_time = 0.0  # type: ignore[union-attr]  # Force plan generation
        bridge._cfg = {"battery_1_kwh": "0", "battery_2_kwh": "0"}  # type: ignore[union-attr]

        mock_nordpool = MagicMock()
        mock_nordpool.today_prices = [50.0] * 24
        mock_nordpool.tomorrow_prices = [55.0] * 24

        mock_solcast = MagicMock()
        mock_solcast.today_hourly_kw = [1.0] * 3  # Short → needs padding (line 612)
        mock_solcast.tomorrow_hourly_kw = []
        mock_solcast.tomorrow_kwh = 5.0

        # Patch classes in the coordinator_bridge module namespace (avoids __new__ pollution)
        with (
            patch(
                "custom_components.carmabox.coordinator_bridge.NordpoolAdapter",
                return_value=mock_nordpool,
            ),
            patch(
                "custom_components.carmabox.coordinator_bridge.SolcastAdapter",
                return_value=mock_solcast,
            ),
        ):
            _asyncio.get_event_loop().run_until_complete(
                bridge._generate_plan()  # type: ignore[union-attr]
            )

    def test_generate_plan_exception_caught(self) -> None:
        """Exception inside _generate_plan → caught, logs (lines 724-725)."""
        import asyncio as _asyncio

        bridge = _make_bridge()
        bridge._last_plan_time = 0.0  # type: ignore[union-attr]

        # Make NordpoolAdapter raise → caught by outer except in _generate_plan
        with patch(
            "custom_components.carmabox.coordinator_bridge.NordpoolAdapter",
            side_effect=RuntimeError("nordpool fail"),
        ):
            _asyncio.get_event_loop().run_until_complete(
                bridge._generate_plan()  # type: ignore[union-attr]
            )
        # Exception caught — plan stays empty
        assert bridge.plan == []  # type: ignore[union-attr]

    # ── _async_update_data paths ────────────────────────────────────────────

    def test_async_update_data_startup_safety_no_adapters(self) -> None:
        """startup_safety_confirmed=False + no adapters → confirms (lines 888-890, 916-917)."""
        from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

        bridge = _make_bridge()
        bridge._startup_safety_confirmed = False  # type: ignore[union-attr]
        bridge._state_restored = True  # type: ignore[union-attr]
        bridge._use_v2 = False  # type: ignore[union-attr]
        bridge._last_plan_time = time.monotonic()  # No plan trigger
        bridge._meter_state = MagicMock()
        bridge._meter_state.hour = -1

        with patch.object(
            CoordinatorBridge,
            "_collect_ha_state",
            return_value=MagicMock(),
        ):
            asyncio.get_event_loop().run_until_complete(
                bridge._async_update_data()  # type: ignore[union-attr]
            )
        # With no adapters, all_off=True → confirmed
        assert bridge._startup_safety_confirmed is True  # type: ignore[union-attr]

    def test_async_update_data_outer_exception_returns_empty(self) -> None:
        """_collect_ha_state raises → outer except returns CarmaboxState (lines 1051-1059)."""
        from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

        bridge = _make_bridge()

        with patch.object(
            CoordinatorBridge,
            "_collect_ha_state",
            side_effect=RuntimeError("sensor crash"),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                bridge._async_update_data()  # type: ignore[union-attr]
            )
        # Should return CarmaboxState() (empty) since data=None
        assert result is not None
        assert bridge._consecutive_errors == 1  # type: ignore[union-attr]

    def test_async_update_data_plan_interval_triggers(self) -> None:
        """_last_plan_time far in past → plan interval fires (lines 1019-1021)."""
        from custom_components.carmabox.coordinator_bridge import CoordinatorBridge

        bridge = _make_bridge()
        bridge._last_plan_time = 0.0  # type: ignore[union-attr]  # Old → triggers plan
        bridge._meter_state = MagicMock()
        bridge._meter_state.hour = -1

        with (
            patch.object(
                CoordinatorBridge,
                "_collect_ha_state",
                return_value=MagicMock(plan=[]),
            ),
            patch.object(
                CoordinatorBridge,
                "_generate_plan",
                new=AsyncMock(),
            ),
        ):
            asyncio.get_event_loop().run_until_complete(
                bridge._async_update_data()  # type: ignore[union-attr]
            )
        # _last_plan_time should have been updated
        assert bridge._last_plan_time > 1.0  # type: ignore[union-attr]
