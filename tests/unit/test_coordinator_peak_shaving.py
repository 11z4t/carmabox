"""EXP-03: Coordinator._apply_reactive_peak_shaving — integration tests.

Verifies the coordinator writes peak_shaving_power_limit (register 47542)
every cycle via GoodWeAdapter.set_peak_shaving_limit(), reactively tracking
actual grid power, only while a battery is already discharging, and that
write/read failures degrade gracefully (fallback) instead of crashing the
update cycle.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.optimizer.models import CarmaboxState
from tests.unit.test_expert_control import _make_coord


def _mock_goodwe_adapter(prefix: str = "kontor", ems_mode: str = "discharge_pv") -> MagicMock:
    """MagicMock standing in for a GoodWeAdapter with peak-shaving support."""
    adapter = MagicMock()
    adapter.prefix = prefix
    adapter.ems_mode = ems_mode
    adapter.set_peak_shaving_limit = AsyncMock(return_value=True)
    return adapter


class TestReactivePeakShavingAppliesDuringDischarge:
    @pytest.mark.asyncio
    async def test_writes_reactive_limit_when_discharge_pv(self) -> None:
        """AC2/AC3: discharge_pv mode → limit = actual_grid + headroom written."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 200.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        state = CarmaboxState(grid_power_w=1500.0)
        await coord._apply_reactive_peak_shaving(state)

        adapter.set_peak_shaving_limit.assert_awaited_once_with(1700)

    @pytest.mark.asyncio
    async def test_writes_reactive_limit_when_discharge_battery(self) -> None:
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 100.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_battery")
        coord.inverter_adapters = [adapter]

        state = CarmaboxState(grid_power_w=800.0)
        await coord._apply_reactive_peak_shaving(state)

        adapter.set_peak_shaving_limit.assert_awaited_once_with(900)

    @pytest.mark.asyncio
    async def test_tracks_load_swing_upward(self) -> None:
        """AC3: house draws MORE → limit rises to match on the next cycle."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 200.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1000.0))
        adapter.set_peak_shaving_limit.assert_awaited_with(1200)

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=3000.0))
        adapter.set_peak_shaving_limit.assert_awaited_with(3200)

    @pytest.mark.asyncio
    async def test_tracks_load_swing_downward(self) -> None:
        """AC3: house draws LESS → limit falls to match on the next cycle."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 200.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=3000.0))
        adapter.set_peak_shaving_limit.assert_awaited_with(3200)

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=400.0))
        adapter.set_peak_shaving_limit.assert_awaited_with(600)


class TestReactivePeakShavingSkipsNonDischarge:
    @pytest.mark.asyncio
    async def test_skips_charge_pv(self) -> None:
        """Never write peak_shaving while charging."""
        coord = _make_coord()
        adapter = _mock_goodwe_adapter(ems_mode="charge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))

        adapter.set_peak_shaving_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_battery_standby(self) -> None:
        coord = _make_coord()
        adapter = _mock_goodwe_adapter(ems_mode="battery_standby")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))

        adapter.set_peak_shaving_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_adapters_only_discharging_one_written(self) -> None:
        """Two batteries: only the discharging one gets a reactive write."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 200.0
        discharging = _mock_goodwe_adapter(prefix="kontor", ems_mode="discharge_pv")
        charging = _mock_goodwe_adapter(prefix="forrad", ems_mode="charge_pv")
        coord.inverter_adapters = [discharging, charging]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1000.0))

        discharging.set_peak_shaving_limit.assert_awaited_once_with(1200)
        charging.set_peak_shaving_limit.assert_not_awaited()


class TestReactivePeakShavingFallback:
    @pytest.mark.asyncio
    async def test_write_failure_does_not_raise(self) -> None:
        """Fallback: adapter write returns False → logged, cycle continues."""
        coord = _make_coord()
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        adapter.set_peak_shaving_limit = AsyncMock(return_value=False)
        coord.inverter_adapters = [adapter]

        # Must not raise
        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))

        adapter.set_peak_shaving_limit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ems_mode_read_exception_skips_adapter(self) -> None:
        """Fallback: reading ems_mode raises → adapter skipped, no crash."""

        class _FlakyAdapter:
            prefix = "kontor"

            @property
            def ems_mode(self) -> str:
                raise RuntimeError("modbus timeout")

            set_peak_shaving_limit = AsyncMock(return_value=True)

        coord = _make_coord()
        adapter = _FlakyAdapter()
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))

        adapter.set_peak_shaving_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adapter_without_peak_shaving_support_skipped(self) -> None:
        """Fallback: non-GoodWe adapters (e.g. Huawei) lack the method — skip cleanly."""
        coord = _make_coord()
        adapter = MagicMock(spec=["prefix", "ems_mode", "set_ems_mode"])
        adapter.prefix = "huawei1"
        adapter.ems_mode = "discharge_pv"
        coord.inverter_adapters = [adapter]

        # Must not raise even though set_peak_shaving_limit doesn't exist
        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))

        assert not hasattr(adapter, "set_peak_shaving_limit")

    @pytest.mark.asyncio
    async def test_no_adapters_configured_is_a_noop(self) -> None:
        coord = _make_coord()
        coord.inverter_adapters = []

        # Must not raise
        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1500.0))


class TestReactivePeakShavingSafetyClamp:
    @pytest.mark.asyncio
    async def test_clamps_extreme_grid_spike_to_10000w(self) -> None:
        """AC4: safety clamp 0-10000W enforced end-to-end through the coordinator."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 200.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=50000.0))

        adapter.set_peak_shaving_limit.assert_awaited_once_with(10000)

    @pytest.mark.asyncio
    async def test_clamps_negative_grid_to_zero(self) -> None:
        """Exporting heavily + small headroom still floors at 0W."""
        coord = _make_coord()
        coord.peak_shaving_headroom_w = 50.0
        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=-5000.0))

        adapter.set_peak_shaving_limit.assert_awaited_once_with(0)


class TestReactivePeakShavingConfigurable:
    @pytest.mark.asyncio
    async def test_headroom_is_configurable_not_hardcoded(self) -> None:
        """peak_shaving_target_headroom_w option flows through to the formula."""
        coord = _make_coord({"peak_shaving_target_headroom_w": 750.0})
        assert coord.peak_shaving_headroom_w == 750.0

        adapter = _mock_goodwe_adapter(ems_mode="discharge_pv")
        coord.inverter_adapters = [adapter]

        await coord._apply_reactive_peak_shaving(CarmaboxState(grid_power_w=1000.0))

        adapter.set_peak_shaving_limit.assert_awaited_once_with(1750)

    def test_headroom_defaults_when_not_configured(self) -> None:
        from custom_components.carmabox.const import DEFAULT_PEAK_SHAVING_TARGET_HEADROOM_W

        coord = _make_coord()
        assert coord.peak_shaving_headroom_w == DEFAULT_PEAK_SHAVING_TARGET_HEADROOM_W
