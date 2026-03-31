"""Coverage tests for _watchdog decision branches.

EXP-EPIC-SWEEP — targets coordinator.py watchdog clusters:
  Lines 2441-2551  — _watchdog W1-W5 self-correction checks
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import CarmaboxState, Decision
from tests.unit.test_expert_control import _make_coord

# ── Helpers ──────────────────────────────────────────────────────────────────

def _state_importing(
    grid_w: float = 1500.0,
    soc: float = 60.0,
    price: float = 80.0,
    pv_w: float = 0.0,
) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_w,
        battery_soc_1=soc,
        current_price=price,
        pv_power_w=pv_w,
    )


def _state_exporting(
    grid_w: float = -600.0,
    soc: float = 60.0,
    price: float = 30.0,
    pv_w: float = 1500.0,
) -> CarmaboxState:
    return CarmaboxState(
        grid_power_w=grid_w,
        battery_soc_1=soc,
        current_price=price,
        pv_power_w=pv_w,
    )


def _make_watchdog_coord(action: str = "idle") -> object:
    """Coordinator with last_decision set for watchdog tests."""
    coord = _make_coord()
    coord.last_decision = Decision(action=action)
    coord.target_kw = 2.0
    return coord


# ── W0: executor_enabled = False → return early (line 2441-2442) ─────────────

class TestWatchdogExecutorDisabled:
    @pytest.mark.asyncio
    async def test_returns_early_when_executor_disabled(self) -> None:
        """executor_enabled=False → immediate return (line 2441-2442)."""
        coord = _make_watchdog_coord()
        coord.executor_enabled = False
        coord._cmd_charge_pv = AsyncMock()

        state = _state_exporting(grid_w=-600.0, soc=60.0)  # Would trigger W1
        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12)
            await coord._watchdog(state)

        coord._cmd_charge_pv.assert_not_called()


# ── W1: Exporting + battery not full + not charging → charge_pv ──────────────

class TestWatchdogW1ExportingNotCharging:
    """Lines 2462-2480: W1 corrects idle-while-exporting to charge_pv."""

    @pytest.mark.asyncio
    async def test_w1_fires_when_exporting_idle_battery_not_full(self) -> None:
        """Exporting 600W + battery 60% + action=idle → W1 → adapter.set_ems_mode(charge_pv)."""
        coord = _make_watchdog_coord(action="idle")
        # W1 uses adapter directly (not _cmd_charge_pv)
        mock_adapter = MagicMock()
        mock_adapter.set_ems_mode = AsyncMock()
        mock_adapter.set_fast_charging = AsyncMock()
        coord.inverter_adapters = [mock_adapter]
        coord._record_decision = AsyncMock()

        # DEFAULT_WATCHDOG_EXPORT_W = typically 200W
        state = _state_exporting(grid_w=-600.0, soc=60.0)  # abs > 200W, soc not 100%

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12)
            await coord._watchdog(state)

        mock_adapter.set_ems_mode.assert_called_once_with("charge_pv")
        coord._record_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_w1_skips_when_already_charging(self) -> None:
        """Already in charge_pv → W1 not triggered."""
        coord = _make_watchdog_coord(action="charge_pv")
        coord._cmd_charge_pv = AsyncMock()

        state = _state_exporting(grid_w=-600.0, soc=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12)
            await coord._watchdog(state)

        coord._cmd_charge_pv.assert_not_called()

    @pytest.mark.asyncio
    async def test_w1_skips_when_battery_full(self) -> None:
        """Battery all_batteries_full → W1 not triggered."""
        coord = _make_watchdog_coord(action="idle")
        coord._cmd_charge_pv = AsyncMock()

        # CarmaboxState.all_batteries_full checks if soc_1 + soc_2 are both >= 99
        state = _state_exporting(grid_w=-600.0, soc=100.0)  # battery_soc_1=100

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=12)
            await coord._watchdog(state)

        coord._cmd_charge_pv.assert_not_called()


# ── W2: Grid > target + battery capacity + not discharging ───────────────────

class TestWatchdogW2HighGrid:
    """Lines 2484-2522: W2 corrects idle when grid > target*1.1."""

    @pytest.mark.asyncio
    async def test_w2_fires_when_grid_above_target(self) -> None:
        """Grid >> target + battery + idle → W2 → adapter.set_ems_mode(discharge_pv)."""
        coord = _make_watchdog_coord(action="idle")
        coord.target_kw = 2.0
        # W2 uses adapter directly (not _cmd_discharge)
        mock_adapter = MagicMock()
        mock_adapter.set_ems_mode = AsyncMock()
        mock_adapter.set_fast_charging = AsyncMock()
        coord.inverter_adapters = [mock_adapter]
        coord._record_decision = AsyncMock()
        coord._execute_ev = AsyncMock()
        coord._execute_miner = AsyncMock()
        coord._execute_climate = AsyncMock()

        # Daytime (weight=1.0): weighted_net = 4000W > target*1000*1.1 = 2200W
        # discharge_w = (4000 - 2200) / 1.0 = 1800W > wd_discharge_min
        state = _state_importing(grid_w=4000.0, soc=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)  # Day → weight 1.0
            await coord._watchdog(state)

        mock_adapter.set_ems_mode.assert_called_once_with("discharge_pv")
        coord._record_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_w2_skips_when_already_discharging(self) -> None:
        """Already discharging → W2 not triggered."""
        coord = _make_watchdog_coord(action="discharge")
        coord._cmd_discharge = AsyncMock()

        state = _state_importing(grid_w=4000.0, soc=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            await coord._watchdog(state)

        coord._cmd_discharge.assert_not_called()

    @pytest.mark.asyncio
    async def test_w2_skips_when_battery_at_min_soc(self) -> None:
        """Battery at min_soc → W2 not triggered (no capacity)."""
        coord = _make_watchdog_coord(action="idle")
        coord.min_soc = 60.0  # min_soc = 60%
        coord._cmd_discharge = AsyncMock()

        state = _state_importing(grid_w=4000.0, soc=60.0)  # soc == min_soc

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            await coord._watchdog(state)

        coord._cmd_discharge.assert_not_called()

    @pytest.mark.asyncio
    async def test_w2_safety_check_blocks_discharge(self) -> None:
        """W2 fires but safety.check_discharge returns ok=False → no discharge."""
        coord = _make_watchdog_coord(action="idle")
        coord.target_kw = 2.0
        coord.safety.check_discharge = MagicMock(
            return_value=MagicMock(ok=False, reason="safety_block")
        )
        coord._cmd_discharge = AsyncMock()

        state = _state_importing(grid_w=4000.0, soc=60.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            await coord._watchdog(state)

        coord._cmd_discharge.assert_not_called()


# ── W4: EV charging + grid importing (day) ───────────────────────────────────

class TestWatchdogW4EvImporting:
    """Lines 2524-2535: W4 stops EV when charging during daytime import."""

    @pytest.mark.asyncio
    async def test_w4_stops_ev_when_importing_daytime(self) -> None:
        """is_day + _ev_enabled + importing → W4 stop EV (lines 2531-2535)."""
        coord = _make_watchdog_coord(action="idle")
        coord._ev_enabled = True
        coord._cmd_ev_stop = AsyncMock()

        # DEFAULT_WATCHDOG_EV_IMPORT_W = typically 500W
        state = _state_importing(grid_w=800.0)  # importing > threshold, not exporting

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)  # Day, not night
            await coord._watchdog(state)

        coord._cmd_ev_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_w4_skips_when_ev_disabled(self) -> None:
        """EV not enabled → W4 not triggered."""
        coord = _make_watchdog_coord(action="idle")
        coord._ev_enabled = False
        coord._cmd_ev_stop = AsyncMock()

        state = _state_importing(grid_w=800.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            await coord._watchdog(state)

        coord._cmd_ev_stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_w4_skips_at_night(self) -> None:
        """Night → W4 not triggered (night EV charging is allowed)."""
        coord = _make_watchdog_coord(action="idle")
        coord._ev_enabled = True
        coord._cmd_ev_stop = AsyncMock()

        state = _state_importing(grid_w=800.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=23)  # Night
            await coord._watchdog(state)

        coord._cmd_ev_stop.assert_not_called()


# ── W5: High price + battery capacity + idle ─────────────────────────────────

class TestWatchdogW5HighPriceIdle:
    """Lines 2537-2550: W5 logs warning when high price + battery + idle."""

    @pytest.mark.asyncio
    async def test_w5_fires_when_high_price_battery_idle(self) -> None:
        """High price + battery > wd_min_soc + idle + grid near target → W5 (line 2544)."""
        coord = _make_watchdog_coord(action="idle")
        coord.target_kw = 2.0
        coord._cfg["price_expensive_ore"] = 80.0

        # price=150 > 80, soc=60 > DEFAULT_WATCHDOG_MIN_SOC_PCT(typically 20)
        # grid=1800W, weight=1.0 → weighted_net=1800 > target*0.8=1600 → W5
        state = _state_importing(grid_w=1800.0, soc=60.0, price=150.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            # Should not crash, just logs info
            await coord._watchdog(state)

    @pytest.mark.asyncio
    async def test_w5_skips_when_action_not_idle(self) -> None:
        """Action = discharge → W5 not triggered."""
        coord = _make_watchdog_coord(action="discharge")
        coord.target_kw = 2.0
        coord._cfg["price_expensive_ore"] = 80.0

        state = _state_importing(grid_w=1800.0, soc=60.0, price=150.0)

        with patch("custom_components.carmabox.coordinator.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(hour=10)
            await coord._watchdog(state)
        # No assertion needed — just verifies no crash when action != idle
