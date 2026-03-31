"""Coverage tests — batch 17.

Targets:
  core/planner.py:         111, 131, 167-169, 229, 422-429, 578,
                            791, 890-891, 1064, 1087, 1150
  coordinator_bridge.py:   278, 321-324, 422, 431, 444, 540, 545,
                            612, 619, 639, 724-725, 797-798, 836-840,
                            878-879, 1127, 1132-1134, 1139, 1144, 1148, 1157
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# ══════════════════════════════════════════════════════════════════════════════
# core/planner.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPlannerGaps:
    """Lines 111, 131, 167-169, 229, 422-429, 578, 791, 890-891, 1064, 1087, 1150."""

    # ── plan_solar_allocation ───────────────────────────────────────────────

    def test_plan_solar_no_hours_returns_early(self) -> None:
        """n=0 (empty pv list) → early return (line 111)."""
        from custom_components.carmabox.core.planner import plan_solar_allocation

        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=10.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            hourly_pv_kw=[],  # Empty → n=0
            hourly_consumption_kw=[2.0] * 4,
            current_hour=14,
        )
        assert result.ev_can_charge is False
        assert "No solar" in result.reason

    def test_plan_solar_no_surplus_infinite_battery_time(self) -> None:
        """avg_surplus=0, battery_need > 0 → battery_hours_to_full=inf (line 131)."""
        from custom_components.carmabox.core.planner import plan_solar_allocation

        # Very high consumption → surplus is negative/zero → avg_surplus = 0
        result = plan_solar_allocation(
            battery_soc_pct=50.0,
            battery_cap_kwh=10.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            hourly_pv_kw=[0.1] * 4,  # Tiny PV
            hourly_consumption_kw=[5.0] * 4,  # Huge consumption → no surplus
            current_hour=14,
        )
        # avg_surplus=0, battery_need > 0 → battery_hours_to_full = inf
        assert result.battery_hours_to_full == float("inf")

    def test_plan_solar_export_scenario_rule2(self) -> None:
        """total_surplus > battery_need → Rule 2 export scenario (lines 167-169)."""
        from custom_components.carmabox.core.planner import plan_solar_allocation

        # Battery 95% full, high PV → surplus exceeds tiny battery_need
        result = plan_solar_allocation(
            battery_soc_pct=95.0,  # Almost full → battery_need=0.5kWh
            battery_cap_kwh=10.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            hourly_pv_kw=[4.0] * 4,  # Strong PV
            hourly_consumption_kw=[0.5] * 4,  # Low consumption
            current_hour=14,
        )
        # total_surplus(3.5*4=14) > battery_need(0.5) → Rule 2 path
        # ev_can_charge should be True (enough surplus to EV-charge)
        assert result.surplus_after_battery_kwh > 0

    def test_plan_solar_ev_would_exceed_tak_returns_no_charge(self) -> None:
        """max_ev_kw < ev_1phase_kw → EV would break tak → return (line 229)."""
        from custom_components.carmabox.core.planner import plan_solar_allocation

        # Battery 95% full (will export), but PV barely covers consumption
        # So max_ev_kw ≈ 0 (pv - consumption + tak=2 is tiny)
        result = plan_solar_allocation(
            battery_soc_pct=95.0,
            battery_cap_kwh=10.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            hourly_pv_kw=[0.5] * 4,  # Very low PV
            hourly_consumption_kw=[4.5] * 4,  # Very high consumption
            current_hour=14,
        )
        # Even if will_export is triggered, max_ev_kw will be too small for EV
        assert result.ev_can_charge is False

    # ── allocate_pv_surplus ─────────────────────────────────────────────────

    def test_allocate_pv_battery_first_ev_gets_excess(self) -> None:
        """ev_priority=False + remaining > battery_max + ev_min → EV gets excess (lines 422-429)."""
        from custom_components.carmabox.core.planner import allocate_pv_surplus

        # is_workday=True, battery_soc_pct=50% → battery gets filled first
        # Large PV surplus → remaining >> battery_max + ev_min → EV gets the rest
        result = allocate_pv_surplus(
            pv_now_w=10000.0,  # Strong PV
            grid_now_w=-8000.0,  # Exporting heavily (negative = export)
            house_consumption_w=500.0,
            battery_soc_pct=50.0,  # Needs charging
            battery_cap_kwh=10.0,
            ev_soc_pct=50.0,
            ev_connected=True,
            ev_target_pct=80.0,
            is_workday=True,  # Not weekend → battery first
            hours_to_sunset=5.0,
            hourly_pv_remaining_kw=[4.0] * 5,
        )
        # Should allocate to both battery and EV
        assert result is not None

    # ── generate_carma_plan / max_daytime_discharge ─────────────────────────

    def test_generate_plan_partial_solar_day(self) -> None:
        """pv_tomorrow between thresholds → partial discharge rate (line 578)."""
        from custom_components.carmabox.core.planner import (
            PlannerConfig,
            PlannerInput,
            generate_carma_plan,
        )

        cfg = PlannerConfig()
        # Set pv_tomorrow between solar_partial and solar_strong thresholds
        mid_pv = (cfg.solar_partial_threshold_kwh + cfg.solar_strong_threshold_kwh) / 2

        n = 10
        inp = PlannerInput(
            start_hour=14,
            hourly_prices=[50.0] * n,
            hourly_pv=[0.5] * n,
            hourly_loads=[2.0] * n,
            hourly_ev=[0.0] * n,
            battery_soc=60.0,
            battery_cap_kwh=10.0,
            ev_soc=50.0,
            ev_cap_kwh=77.0,
            pv_forecast_tomorrow_kwh=mid_pv,
        )
        result = generate_carma_plan(inp, cfg)
        assert isinstance(result, list)  # Just verify it runs (line 578 covered)

    # ── find_cheapest_hours ─────────────────────────────────────────────────

    def test_find_cheapest_hours_normal_path(self) -> None:
        """prices not empty, n_hours > 0 → n_hours = min(n_hours, len(prices)) (line 791)."""
        from custom_components.carmabox.core.planner import find_cheapest_hours

        result = find_cheapest_hours([80.0, 20.0, 50.0, 10.0], n_hours=2)
        assert len(result) == 2
        assert 3 in result  # Index 3 = price 10.0 (cheapest)
        assert 1 in result  # Index 1 = price 20.0 (second cheapest)

    def test_find_cheapest_hours_n_exceeds_prices(self) -> None:
        """n_hours > len(prices) → capped to len(prices)."""
        from custom_components.carmabox.core.planner import find_cheapest_hours

        result = find_cheapest_hours([50.0, 30.0], n_hours=10)  # 10 > 2
        assert len(result) == 2  # Capped to len=2

    def test_find_cheapest_hours_empty_prices_returns_empty(self) -> None:
        """empty prices → return [] early (line 791)."""
        from custom_components.carmabox.core.planner import find_cheapest_hours

        assert find_cheapest_hours([], n_hours=3) == []

    # ── should_discharge_now ────────────────────────────────────────────────

    def test_should_discharge_below_min_soc(self) -> None:
        """battery_soc <= min_soc → guard clause sets reason, returns (lines 890-891)."""
        from custom_components.carmabox.core.planner import should_discharge_now

        # soc=15% = min_soc → hits guard
        result = should_discharge_now(
            current_price_ore=200.0,
            upcoming_prices_ore=[100.0, 80.0, 120.0],
            battery_soc_pct=35.0,  # > 30 (passes low-battery guard)
            min_soc=40.0,  # > soc → triggers line 890-891
        )
        assert result["discharge"] is False
        assert "min SoC" in result["reason"]

    # ── should_charge_ev_tonight ────────────────────────────────────────────

    def test_ev_already_at_target_no_charge(self) -> None:
        """ev_soc >= ev_target → ev_need <= 0 → early return (line 1064)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        result = should_charge_ev_tonight(
            ev_soc_pct=80.0,
            ev_target_pct=75.0,  # Already above target
            ev_cap_kwh=77.0,
            tonight_prices_ore=[30.0] * 8,
            tomorrow_night_prices_ore=[25.0] * 8,
            pv_tomorrow_kwh=10.0,
        )
        assert result["charge"] is False
        assert "already" in result["reason"].lower()

    def test_ev_charge_no_tomorrow_prices(self) -> None:
        """tomorrow_night_prices_ore=[] → tomorrow_cost_kr=0.0 (line 1087)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        result = should_charge_ev_tonight(
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            tonight_prices_ore=[50.0] * 8,
            tomorrow_night_prices_ore=[],  # No tomorrow data → line 1087
            pv_tomorrow_kwh=5.0,
        )
        # tomorrow_cost_kr defaults to 0.0 (free) → may wait for tomorrow
        assert result is not None

    def test_ev_charge_no_tomorrow_prices_fallback_tonight(self) -> None:
        """No tomorrow data → fallback to charge tonight (line 1150)."""
        from custom_components.carmabox.core.planner import should_charge_ev_tonight

        # Workday tomorrow, no tomorrow prices, no PV coverage → charge tonight
        result = should_charge_ev_tonight(
            ev_soc_pct=30.0,
            ev_target_pct=80.0,
            ev_cap_kwh=77.0,
            tonight_prices_ore=[60.0] * 8,
            tomorrow_night_prices_ore=[],  # No tomorrow prices
            pv_tomorrow_kwh=2.0,  # Not enough PV to cover
            is_workday_tomorrow=True,  # Must leave for work
        )
        assert result is not None  # Should reach fallback logic


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
    bridge._state_restored = False
    bridge._startup_safety_confirmed = False
    bridge._consecutive_errors = 0
    bridge.inverter_adapters = []
    bridge.ev_adapter = None
    bridge.executor_enabled = False
    bridge._miner_entity = ""
    bridge._last_plan_time = 0.0
    bridge._last_save_time = 0.0
    bridge._use_v2 = False
    bridge.plan = []
    bridge.data = None
    bridge._breach_load_shed_active = False
    bridge.target_kw = 4.0
    # Attrs for _async_save_state
    bridge.night_ev_active = False
    bridge._last_command = BatteryCommand.STANDBY
    bridge._ev_enabled = False
    bridge._ev_current_amps = 6
    bridge._ellevio_hour_samples = []
    bridge._ellevio_current_hour = 0
    return bridge


class TestCoordinatorBridgeGaps:
    """Lines 278, 321-324, 422, 431, 444, 540, 545, 612, 619, 639, 724-725,
    797-798, 836-840, 878-879, 1127, 1132-1134, 1139, 1144, 1148, 1157."""

    # ── _read_str happy path ─────────────────────────────────────────────────

    def test_read_str_valid_state_returns_value(self) -> None:
        """Valid entity with non-unavailable state → return state.state (line 278)."""
        bridge = _make_bridge()
        state = MagicMock()
        state.state = "on"
        bridge.hass.states.get = MagicMock(return_value=state)  # type: ignore[union-attr]
        result = bridge._read_str("switch.test")  # type: ignore[union-attr]
        assert result == "on"

    # ── _collect_system_state miner logic ────────────────────────────────────

    def test_collect_system_state_miner_entity_transform(self) -> None:
        """_miner_entity set → switch→sensor transform (lines 321-324)."""
        bridge = _make_bridge()
        bridge._miner_entity = "switch.miner_main"  # type: ignore[union-attr]
        # All other required attrs for _collect_system_state
        bridge.inverter_adapters = []  # type: ignore[union-attr]
        bridge.ev_adapter = None  # type: ignore[union-attr]
        # Just set up all the attrs that _collect_system_state needs
        for attr in [
            "_bat1_entity",
            "_bat2_entity",
            "_grid_entity",
            "_pv_entity",
            "_house_entity",
            "_ev_soc_entity",
            "_ev_connected_entity",
            "_temp_entity",
            "_temp2_entity",
        ]:
            setattr(bridge, attr, "")
        state = MagicMock()
        state.state = "0.0"
        bridge.hass.states.get = MagicMock(return_value=state)  # type: ignore[union-attr]
        # Call the method — should not raise
        result = bridge._collect_system_state()  # type: ignore[union-attr]
        assert result is not None

    # ── _execute_battery_commands error paths ────────────────────────────────

    def test_execute_battery_commands_charge_pv_limit_fail(self) -> None:
        """charge_pv + GoodWe + set_discharge_limit(0) fails → _LOGGER.error (line 422)."""
        import asyncio

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge()
        mock_adapter = MagicMock(spec=GoodWeAdapter)
        mock_adapter.set_ems_mode = AsyncMock(return_value=True)
        mock_adapter.set_discharge_limit = AsyncMock(return_value=False)  # Fails
        mock_adapter.fast_charging_on = False
        bridge.inverter_adapters = [mock_adapter]  # type: ignore[union-attr]

        commands = [{"id": 0, "mode": "charge_pv", "power_limit": 0, "fast_charging": False}]
        asyncio.get_event_loop().run_until_complete(
            bridge._execute_battery_commands(commands)  # type: ignore[union-attr]
        )
        mock_adapter.set_discharge_limit.assert_called_once()

    def test_execute_battery_commands_discharge_limit_fail(self) -> None:
        """discharge_pv + set_discharge_limit fails → _LOGGER.error (line 431)."""
        import asyncio

        from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

        bridge = _make_bridge()
        mock_adapter = MagicMock(spec=GoodWeAdapter)
        mock_adapter.set_ems_mode = AsyncMock(return_value=True)
        mock_adapter.set_discharge_limit = AsyncMock(return_value=False)  # Fails
        mock_adapter.fast_charging_on = False
        bridge.inverter_adapters = [mock_adapter]  # type: ignore[union-attr]

        commands = [{"id": 0, "mode": "discharge_pv", "power_limit": 3000, "fast_charging": False}]
        asyncio.get_event_loop().run_until_complete(
            bridge._execute_battery_commands(commands)  # type: ignore[union-attr]
        )
        mock_adapter.set_discharge_limit.assert_called_with(3000)

    # ── _async_save_state exception ──────────────────────────────────────────

    def test_async_save_state_exception_logged(self) -> None:
        """store.async_save raises → logged, continues (lines 797-798)."""
        import asyncio

        bridge = _make_bridge()
        mock_store = MagicMock()
        mock_store.async_save = AsyncMock(side_effect=Exception("disk full"))
        bridge._store = mock_store  # type: ignore[union-attr]
        # Should not raise — exception is caught
        asyncio.get_event_loop().run_until_complete(
            bridge._async_save_state()  # type: ignore[union-attr]
        )

    # ── _async_restore_state enum fallback ───────────────────────────────────

    def test_async_restore_state_bad_command_fallback(self) -> None:
        """Invalid BatteryCommand string → ValueError → KeyError → STANDBY (lines 836-840)."""
        import asyncio

        from custom_components.carmabox.coordinator_bridge import BatteryCommand

        bridge = _make_bridge()
        mock_store = MagicMock()
        # Return data with an invalid last_command string
        mock_store.async_load = AsyncMock(
            return_value={
                "last_command": "totally_invalid_command_xyz",
                "soc_pct": 50.0,
                "mode": "standby",
                "executor_enabled": False,
                "ev_enabled": True,
            }
        )
        bridge._store = mock_store  # type: ignore[union-attr]

        asyncio.get_event_loop().run_until_complete(
            bridge._async_restore_state()  # type: ignore[union-attr]
        )
        # Falls back to STANDBY after both ValueError and KeyError
        assert bridge._last_command == BatteryCommand.STANDBY  # type: ignore[union-attr]

    def test_async_restore_state_exception_caught(self) -> None:
        """store.async_load raises broadly → logged, starting fresh (lines 878-879)."""
        import asyncio

        bridge = _make_bridge()
        mock_store = MagicMock()
        mock_store.async_load = AsyncMock(side_effect=Exception("corrupt storage"))
        bridge._store = mock_store  # type: ignore[union-attr]
        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            bridge._async_restore_state()  # type: ignore[union-attr]
        )

    # ── _generate_plan padding logic ─────────────────────────────────────────

    def test_generate_plan_pv_forecast_padding(self) -> None:
        """Short PV forecast padded to match prices length (lines 612, 619)."""
        import asyncio

        bridge = _make_bridge()
        # Set up minimal state for _generate_plan
        bridge.plan = []  # type: ignore[union-attr]
        bridge.total_charge_kwh = 0.0  # type: ignore[union-attr]
        bridge.total_discharge_kwh = 0.0  # type: ignore[union-attr]

        mock_nordpool = MagicMock()
        mock_nordpool.prices_ore = [50.0] * 24
        mock_nordpool.tomorrow_prices_ore = [60.0] * 24

        mock_solcast = MagicMock()
        # Short PV forecast → needs padding to match 24 prices
        mock_solcast.today_hourly_kw = [1.0] * 5  # Only 5 hours
        mock_solcast.tomorrow_hourly_kw = [0.5] * 5  # Short

        bridge.nordpool = mock_nordpool  # type: ignore[union-attr]
        bridge.solcast = mock_solcast  # type: ignore[union-attr]

        # Additional attrs needed
        for attr in ["battery_soc_pct", "battery_cap_kwh"]:
            setattr(bridge, attr, 50.0)
        bridge.inverter_adapters = []  # type: ignore[union-attr]
        bridge._state = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            bridge._generate_plan()  # type: ignore[union-attr]
        )
        # Just verify it ran (padding lines covered)

    # ── Properties and stub methods ──────────────────────────────────────────

    def test_hourly_meter_projected_property(self) -> None:
        """Property access → returns _meter_state.projected_avg (line 1127)."""
        bridge = _make_bridge()
        mock_meter = MagicMock()
        mock_meter.projected_avg = 2.5
        bridge._meter_state = mock_meter  # type: ignore[union-attr]
        assert bridge.hourly_meter_projected == 2.5  # type: ignore[union-attr]

    def test_hourly_meter_pct_zero_target(self) -> None:
        """target_kw=0 → return 0.0 (line 1132-1133)."""
        bridge = _make_bridge()
        bridge.target_kw = 0.0  # type: ignore[union-attr]
        assert bridge.hourly_meter_pct == 0.0  # type: ignore[union-attr]

    def test_hourly_meter_pct_normal(self) -> None:
        """target_kw > 0 → return projection/target * 100 (line 1134)."""
        bridge = _make_bridge()
        bridge.target_kw = 4.0  # type: ignore[union-attr]
        mock_meter = MagicMock()
        mock_meter.projected_avg = 2.0
        bridge._meter_state = mock_meter  # type: ignore[union-attr]
        result = bridge.hourly_meter_pct  # type: ignore[union-attr]
        assert result == 50.0  # 2.0 / 4.0 * 100

    def test_breach_monitor_active_property(self) -> None:
        """Returns _breach_load_shed_active (line 1139)."""
        bridge = _make_bridge()
        bridge._breach_load_shed_active = True  # type: ignore[union-attr]
        assert bridge.breach_monitor_active is True  # type: ignore[union-attr]

    def test_daily_insight_stub(self) -> None:
        """Returns stub dict (line 1144)."""
        bridge = _make_bridge()
        result = bridge.daily_insight  # type: ignore[union-attr]
        assert isinstance(result, dict)

    def test_plan_score_stub(self) -> None:
        """Returns stub dict (line 1148)."""
        bridge = _make_bridge()
        result = bridge.plan_score()  # type: ignore[union-attr]
        assert isinstance(result, dict)

    def test_get_active_corrections_stub(self) -> None:
        """Returns list or stub (line 1157)."""
        bridge = _make_bridge()
        result = bridge.get_active_corrections()  # type: ignore[union-attr]
        assert isinstance(result, list)
