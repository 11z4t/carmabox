"""Tests for CARMA Box data models."""

from custom_components.carmabox.optimizer.models import CarmaboxState


class TestCarmaboxState:
    def test_is_exporting_negative_grid(self) -> None:
        state = CarmaboxState(grid_power_w=-500)
        assert state.is_exporting is True

    def test_is_not_exporting_positive_grid(self) -> None:
        state = CarmaboxState(grid_power_w=1000)
        assert state.is_exporting is False

    def test_is_not_exporting_zero(self) -> None:
        state = CarmaboxState(grid_power_w=0)
        assert state.is_exporting is False

    def test_has_battery_2_present(self) -> None:
        state = CarmaboxState(battery_soc_2=50)
        assert state.has_battery_2 is True

    def test_has_battery_2_absent(self) -> None:
        state = CarmaboxState(battery_soc_2=-1)
        assert state.has_battery_2 is False

    def test_has_ev_present(self) -> None:
        state = CarmaboxState(ev_soc=50)
        assert state.has_ev is True

    def test_has_ev_absent(self) -> None:
        state = CarmaboxState(ev_soc=-1)
        assert state.has_ev is False

    def test_all_batteries_full_single(self) -> None:
        state = CarmaboxState(battery_soc_1=100, battery_soc_2=-1)
        assert state.all_batteries_full is True

    def test_all_batteries_full_dual(self) -> None:
        state = CarmaboxState(battery_soc_1=100, battery_soc_2=100)
        assert state.all_batteries_full is True

    def test_not_all_full_one_low(self) -> None:
        state = CarmaboxState(battery_soc_1=100, battery_soc_2=80)
        assert state.all_batteries_full is False

    def test_total_soc_single(self) -> None:
        state = CarmaboxState(battery_soc_1=80, battery_soc_2=-1)
        assert state.total_battery_soc == 80

    def test_total_soc_dual_weighted(self) -> None:
        """Weighted SoC: 80%×15kWh + 60%×5kWh = 75%."""
        state = CarmaboxState(
            battery_soc_1=80, battery_soc_2=60, battery_cap_1_kwh=15, battery_cap_2_kwh=5
        )
        assert state.total_battery_soc == 75.0

    def test_defaults(self) -> None:
        state = CarmaboxState()
        assert state.grid_power_w == 0
        assert state.battery_soc_1 == 0
        assert state.battery_temp_c is None
        assert state.target_weighted_kw == 2.0
        assert state.plan == []

    def test_battery_temp_set(self) -> None:
        state = CarmaboxState(battery_temp_c=25.0)
        assert state.battery_temp_c == 25.0

    def test_battery_temp_none(self) -> None:
        state = CarmaboxState(battery_temp_c=None)
        assert state.battery_temp_c is None
