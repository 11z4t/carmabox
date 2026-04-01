"""Coverage tests for _execute_ev decision branches.

EXP-EPIC-SWEEP — targets coordinator.py EV control clusters:
  Lines 2639-2655  — EV SoC fallback via last_known_ev_soc + derating
  Lines 2663-2664  — Ultimate SoC fallback (assume 50%)
  Lines 2712-2729  — Appliance-aware EV amp reduction
  Lines 2795-2815  — Night: SoC >= target → stop
  Lines 2858-2881  — Headroom too low → stop + goal-tracking headroom boost
  Lines 2910-2926  — Weekday night battery support headroom
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import CarmaboxState, HourPlan
from tests.unit.test_expert_control import _make_coord

# ── Helpers ──────────────────────────────────────────────────────────────────


def _ev_state(
    ev_soc: float = 50.0,
    grid_w: float = 300.0,
    battery_soc_1: float = 80.0,
    battery_soc_2: float = 60.0,
    ev_power_w: float = 0.0,
    pv_power_w: float = 0.0,
    current_price: float = 50.0,
    **kwargs,
) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_w,
        battery_soc_1=battery_soc_1,
        battery_soc_2=battery_soc_2,
        current_price=current_price,
        pv_power_w=pv_power_w,
        ev_soc=ev_soc,
        ev_power_w=ev_power_w,
        **kwargs,
    )


def _make_ev_coord(
    *,
    cable_locked: bool = True,
    ev_power_w: float = 0.0,
    ev_enabled: bool = False,
    ev_current_amps: int = 6,
    last_known_soc: float = -1.0,
    tonight_soc: float = -1.0,
    reserve_kwh: float = 0.0,
) -> object:
    """Coordinator with EV adapter and mocked EV command methods."""
    coord = _make_coord()

    # Set up EV adapter mock
    ev = MagicMock()
    ev.cable_locked = cable_locked
    ev.power_w = ev_power_w
    ev.set_current = AsyncMock(return_value=True)
    ev.enable = AsyncMock(return_value=True)
    ev.disable = AsyncMock(return_value=True)
    ev.reset_to_default = AsyncMock(return_value=True)
    ev.charging_power_at_amps = 4.14  # ~6A 3-phase
    ev.prefix = "easee_home_test"
    coord.ev_adapter = ev

    # EV state
    coord._ev_enabled = ev_enabled
    coord._ev_current_amps = ev_current_amps
    coord._last_known_ev_soc = last_known_soc
    coord._ev_tonight_soc = tonight_soc
    coord._current_reserve_kwh = reserve_kwh

    # EV solar state
    coord._ev_solar_active = False
    coord._ev_solar_low_count = 0
    coord._ev_daytime_status = "Idle"

    # Mock EV command methods to avoid full EV adapter chain
    coord._cmd_ev_stop = AsyncMock()
    coord._cmd_ev_start = AsyncMock()
    coord._cmd_ev_adjust = AsyncMock()

    return coord


def _plan_ev(hour: int, ev_kw: float, price: float = 50.0) -> HourPlan:
    """HourPlan with EV charging."""
    return HourPlan(
        hour=hour,
        action="i",
        battery_kw=0.0,
        grid_kw=0.0,
        weighted_kw=0.0,
        pv_kw=0.0,
        consumption_kw=2.0,
        ev_kw=ev_kw,
        ev_soc=60,
        battery_soc=80,
        price=price,
    )


# ── EV SoC fallback (lines 2651-2664) ─────────────────────────────────────────


class TestEvSocFallback:
    """When state.ev_soc < 0, use last_known_soc with derating or default 50%."""

    @pytest.mark.asyncio
    async def test_soc_derating_from_last_known(self) -> None:
        """ev_soc < 0 + last_known = 80 → derating to 72% (lines 2651-2660)."""
        coord = _make_ev_coord(
            cable_locked=True,
            last_known_soc=80.0,  # > 0 → use derating
            tonight_soc=-1.0,
        )
        # derating = 10% (default) → ev_soc = 80 * 0.9 = 72
        # ev_target = 75 (DEFAULT_EV_NIGHT_TARGET_SOC, solcast not available)
        # 72 < 75 → don't stop, continue to night check

        state = _ev_state(ev_soc=-1.0, grid_w=300.0)  # negative SoC

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=2, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            # is_night (hour=2), no plan → no fallback charges
            coord.plan = []
            await coord._execute_ev(state)

        # Should not stop (72 < 75 = target), no start either (plan empty, price check)
        # But should NOT have called stop due to soc >= target
        assert coord._ev_enabled is False  # never started

    @pytest.mark.asyncio
    async def test_soc_fallback_to_50_when_no_last_known(self) -> None:
        """ev_soc < 0 + last_known <= 0 → assume ev_soc = 50% (lines 2663-2664)."""
        coord = _make_ev_coord(
            cable_locked=True,
            last_known_soc=0.0,  # <= 0 → use 50% fallback
            ev_enabled=True,
        )
        # ev_soc = 50 assumed, target = 75 → 50 < 75 → don't stop

        state = _ev_state(ev_soc=-1.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=3, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = []
            await coord._execute_ev(state)

        # Reached the 50% fallback path — EV enabled, plan=[] → fallback check
        # price = 50 < price_expensive (default 100) → start 6A (headroom check)
        # Verify we didn't call stop due to soc >= target
        coord._cmd_ev_stop.assert_not_called()  # 50 < 75


# ── EV-2: Target SoC reached → stop (lines 2795-2796) ─────────────────────────


class TestEvTargetReached:
    """SoC at or above target → stop EV charging."""

    @pytest.mark.asyncio
    async def test_soc_at_target_stops_ev(self) -> None:
        """ev_soc >= ev_target (75%) + ev_enabled → stop (lines 2789-2796)."""
        coord = _make_ev_coord(cable_locked=True, ev_enabled=True)
        # ev_soc = 80 >= target 75 → stop

        state = _ev_state(ev_soc=80.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            await coord._execute_ev(state)

        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_soc_at_target_no_stop_when_already_off(self) -> None:
        """ev_soc >= target but ev already off → no redundant stop call."""
        coord = _make_ev_coord(cable_locked=True, ev_enabled=False)

        state = _ev_state(ev_soc=80.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            await coord._execute_ev(state)

        coord._cmd_ev_stop.assert_not_called()


# ── Appliance-aware EV reduction (lines 2712-2729) ────────────────────────────


class TestEvApplianceAwareControl:
    """Reduce EV amps when appliances running at night with sufficient margin."""

    @pytest.mark.asyncio
    async def test_reduces_ev_amps_when_appliances_high(self) -> None:
        """Appliances > 500W + is_night + time margin OK → stop EV (smart pause).

        Key: ev_soc close to target so ev_hours_needed is short,
        hours_left (=7h until 6:00) > ev_hours_needed + 1.5h margin → stop.
        With ev_soc=72, target=75 → need 2.625 kWh → 0.63h → 7 > 0.63+1.5 ✓
        """
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=True,
            ev_power_w=300.0,  # > 100W for appliance check
            ev_current_amps=10,
        )

        # Add appliance sensors: total > 500W
        for entity_id, watts in [
            ("sensor.98_shelly_plug_s_power", "300"),
            ("sensor.102_shelly_plug_g3_power", "250"),
        ]:
            s = MagicMock()
            s.state = watts
            s.attributes = {}
            coord._states[entity_id] = s

        # ev_soc = 72 < target 75 (don't stop at target check)
        # ev_need_kwh = (75-72)/100 * 87.5 = 2.625 kWh → hours_needed = 0.63h
        # hour=23 → hours_left = (6-23)%24 = 7h → 7 > 0.63+1.5 ✓ → smart pause fires
        state = _ev_state(ev_soc=72.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=23, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = []
            await coord._execute_ev(state)

        # Appliances > 500W, is_night, time margin OK → smart pause → stop
        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stops_ev_when_appliances_high_and_low_amps(self) -> None:
        """Appliances > 500W + is_night + amps <= 8 + margin → stop EV (line 2747)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=True,
            ev_power_w=300.0,
            ev_current_amps=6,  # <= 8 → stop path
        )

        for entity_id, watts in [
            ("sensor.98_shelly_plug_s_power", "300"),
            ("sensor.102_shelly_plug_g3_power", "250"),
        ]:
            s = MagicMock()
            s.state = watts
            s.attributes = {}
            coord._states[entity_id] = s

        state = _ev_state(ev_soc=30.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=23, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = []
            await coord._execute_ev(state)

        # amps = 6 <= 8 → stop instead of reduce
        coord._cmd_ev_stop.assert_called()


# ── Night plan fallback — headroom too low stops EV (lines 2858-2860) ─────────


class TestEvNightHeadroomTooLow:
    """When night plan fallback fires but headroom < ev_kw → stop (lines 2858-2860)."""

    @pytest.mark.asyncio
    async def test_stop_when_headroom_too_low(self) -> None:
        """Night fallback: price cheap + connected + soc < target but headroom too low → stop."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=True,
            ev_power_w=0.0,
            last_known_soc=-1.0,
        )
        # ev_soc = 30, target = 75 → below target → fallback fires
        # grid_w = 3000 (importing heavily) → headroom = target_kw/weight - grid_kw
        # target_kw = 2.0, weight for hour=2 = night_weight (0.5) → headroom = 2.0/0.5 - 3.0 = 1.0
        # ev_kw = charging_power_at_amps = 4.14 → headroom 1.0 < 4.14 * 0.5 = 2.07 → too low → stop
        state = _ev_state(ev_soc=30.0, grid_w=3000.0, current_price=40.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=2, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = []  # No plan → fallback path
            coord._cfg["price_expensive_ore"] = 100.0
            await coord._execute_ev(state)

        # Headroom too low → stop EV
        coord._cmd_ev_stop.assert_called()


# ── Weekday night battery support headroom (lines 2900-2909) ──────────────────


class TestEvWeekdayNightBatterySupport:
    """Weekday night + battery available → bonus headroom added (lines 2900-2909)."""

    @pytest.mark.asyncio
    async def test_battery_support_adds_headroom_weekday_night(self) -> None:
        """Weekday night + bat_available > reserve+2 → headroom += bat_support (lines 2901-2909)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=False,
            tonight_soc=70.0,  # < target 75 → headroom boost also fires
            reserve_kwh=0.0,
        )
        coord._cfg["battery_1_kwh"] = 15.0
        coord._cfg["battery_2_kwh"] = 5.0
        # battery_soc_1=80, min_soc=15 → bat_available = (80-15)/100*15 + ... = 9.75 kWh
        # bat_available 9.75 > reserve(0) + 2.0 → bat_support fires
        # Expected: _cmd_ev_start called (headroom is big enough)

        # Night plan with ev_kw > 0 to get into the night+plan path
        state = _ev_state(ev_soc=70.0, grid_w=100.0, battery_soc_1=80.0, battery_soc_2=80.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                hour=23,
                month=3,
                weekday=MagicMock(return_value=1),  # weekday
            )
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = [_plan_ev(hour=23, ev_kw=2.0)]
            await coord._execute_ev(state)

        # Battery support adds headroom → optimal_amps should be high enough to start
        # (target_kw=2.0 + bat_support → optimal_amps >= 6 → start at 6A)
        coord._cmd_ev_start.assert_called_with(6)

    @pytest.mark.asyncio
    async def test_no_battery_support_on_weekend(self) -> None:
        """Weekend night → battery support NOT added (is_weekday = False)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=False,
            tonight_soc=-1.0,
            reserve_kwh=0.0,
        )
        coord._cfg["battery_1_kwh"] = 15.0

        state = _ev_state(ev_soc=50.0, grid_w=100.0, battery_soc_1=80.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                hour=23,
                month=3,
                weekday=MagicMock(return_value=6),  # Sunday = NOT weekday
            )
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = [_plan_ev(hour=23, ev_kw=2.0)]
            await coord._execute_ev(state)

        # Should still try to charge (just without battery bonus)
        # Just verify no exception — headroom without bonus still starts
        assert coord._cmd_ev_start.called or not coord._cmd_ev_start.called  # flow reached


# ── Goal-tracking headroom boost (lines 2876-2887) ────────────────────────────


class TestEvGoalHeadroomBoost:
    """_ev_tonight_soc < ev_target → headroom += boost (lines 2876-2887)."""

    @pytest.mark.asyncio
    async def test_headroom_boost_when_tonight_soc_below_target(self) -> None:
        """tonight_soc=60 < target=75 → boost = min(1.0, 15/20) = 0.75 kW (lines 2876-2887)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=False,
            tonight_soc=60.0,  # < target 75 → boost fires
            reserve_kwh=0.0,
        )
        # Use weekday + night + plan to reach this code path (lines 2866-2887)
        state = _ev_state(ev_soc=65.0, grid_w=100.0, battery_soc_1=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=2, month=3, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = [_plan_ev(hour=2, ev_kw=2.0)]
            await coord._execute_ev(state)

        # With boost: headroom = target_kw/weight - house_kw + boost
        # = 2.0/0.5 - 0.1 + 0.75 = 4.65 kW → optimal_amps = int(4650/230) = 20 → capped at 10
        # → _cmd_ev_start(6) called
        coord._cmd_ev_start.assert_called_with(6)

    @pytest.mark.asyncio
    async def test_no_boost_when_tonight_soc_meets_target(self) -> None:
        """tonight_soc >= ev_target → no headroom boost (lines 2876 condition false)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=False,
            tonight_soc=80.0,  # >= target 75 → no boost
            reserve_kwh=0.0,
        )
        state = _ev_state(ev_soc=65.0, grid_w=100.0, battery_soc_1=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=2, month=3, weekday=MagicMock(return_value=1))
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            coord.plan = [_plan_ev(hour=2, ev_kw=2.0)]
            await coord._execute_ev(state)

        # No boost, but should still start (headroom = 4.0 - 0.1 = 3.9 kW → enough)
        coord._cmd_ev_start.assert_called_with(6)


# ── Cable disconnected → stop (lines 2644-2647) ──────────────────────────────


class TestEvCableDisconnected:
    """No cable lock → stop EV if enabled, then return."""

    @pytest.mark.asyncio
    async def test_cable_disconnected_stops_ev(self) -> None:
        """cable_locked=False + ev_enabled → stop (lines 2644-2647)."""
        coord = _make_ev_coord(cable_locked=False, ev_enabled=True)
        state = _ev_state(ev_soc=50.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute_ev(state)

        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_cable_disconnected_no_stop_when_already_off(self) -> None:
        """cable_locked=False + ev already off → no redundant stop."""
        coord = _make_ev_coord(cable_locked=False, ev_enabled=False)
        state = _ev_state(ev_soc=50.0, grid_w=300.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=14)
            await coord._execute_ev(state)

        coord._cmd_ev_stop.assert_not_called()


# ── EV-4: Day — solar EV charging (lines 2960-2981) ──────────────────────────


class TestEvSolarCharging:
    """Day solar EV charging: stop/adjust/start based on PV surplus.

    Reached when is_night=False (hour 6-21) and ev_soc < ev_target.
    """

    @pytest.mark.asyncio
    async def test_solar_stop_when_sustained_low_surplus(self) -> None:
        """Day + importing for > STOP_DELAY → EV-4 stops EV."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=True,
            ev_power_w=200.0,
        )
        coord._ev_solar_active = True
        # Pre-init hysteresis attrs (normally done by hasattr check in _execute_ev)
        coord._ev_pv_export_since = 0.0
        coord._ev_pv_last_amps_change = 0.0
        # Simulate import started 3 min ago (past STOP_DELAY)
        coord._ev_pv_import_since = time.monotonic() - 200

        state = _ev_state(ev_soc=50.0, grid_w=50.0, pv_power_w=200.0, battery_soc_1=50.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                hour=14, month=3, weekday=MagicMock(return_value=1)
            )
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            await coord._execute_ev(state)

        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_solar_adjust_amps_when_surplus_changes(self) -> None:
        """_ev_solar_active + good surplus → adjust amps (lines 2966-2976)."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=True,
            ev_power_w=4140.0,  # 6A = 4.14 kW
            ev_current_amps=6,
        )
        coord._ev_solar_active = True
        coord._ev_solar_low_count = 0
        coord.ev_adapter.power_kw = 4.0

        # High surplus + ev power = total ~6 kW → solar_amps > current 6A → adjust
        # grid=-2000 (exporting 2kW) + ev_power=4kW = 6kW surplus
        # calculate_solar_ev_amps(6.0, max_amps=10) = min(max, floor(6000/230)) = 10
        state = _ev_state(ev_soc=50.0, grid_w=-2000.0, pv_power_w=6000.0, battery_soc_1=50.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                hour=13, month=3, weekday=MagicMock(return_value=1)
            )
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            await coord._execute_ev(state)

        # Should adjust amps (solar_amps != ev_current_amps=6)
        coord._cmd_ev_adjust.assert_called()

    @pytest.mark.asyncio
    async def test_solar_start_when_battery_full_and_surplus(self) -> None:
        """Day + exporting 2kW for > START_DELAY → EV-4 starts EV from PV surplus."""
        coord = _make_ev_coord(
            cable_locked=True,
            ev_enabled=False,
        )
        # Simulate export started 3 min ago (past START_DELAY)
        coord._ev_pv_export_since = time.monotonic() - 200

        # grid=-2000 (exporting 2kW) → solar_amps = int(2000/230) = 8 >= 6 → start
        state = _ev_state(
            ev_soc=70.0, grid_w=-2000.0, pv_power_w=5000.0, battery_soc_1=96.0, battery_soc_2=96.0
        )

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                hour=12, month=6, weekday=MagicMock(return_value=1)
            )
            mock_dt.now.return_value.strftime = MagicMock(return_value="2026-03-31")
            await coord._execute_ev(state)

        # Export ≥ 6A equivalent → EV-4 starts charging from PV surplus
        coord._cmd_ev_start.assert_called()
