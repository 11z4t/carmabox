"""Tests for Battery Balancer — proportional discharge/charge."""

from __future__ import annotations

from custom_components.carmabox.core.battery_balancer import (
    BatteryInfo,
    available_kwh,
    calculate_proportional_charge,
    calculate_proportional_discharge,
    effective_min_soc,
)


def _bat(
    id: str = "kontor",
    soc: float = 50,
    cap_kwh: float = 15.0,
    cell_temp_c: float = 15.0,
    min_soc: float = 15.0,
    min_soc_cold: float = 20.0,
    cold_temp_c: float = 4.0,
    max_discharge_w: float = 5000.0,
) -> BatteryInfo:
    return BatteryInfo(
        id=id, soc=soc, cap_kwh=cap_kwh, cell_temp_c=cell_temp_c,
        min_soc=min_soc, min_soc_cold=min_soc_cold,
        cold_temp_c=cold_temp_c, max_discharge_w=max_discharge_w,
    )


class TestEffectiveMinSoc:
    def test_normal_temp(self):
        bat = _bat(cell_temp_c=15.0)
        assert effective_min_soc(bat) == 15.0

    def test_cold_temp(self):
        bat = _bat(cell_temp_c=3.0)
        assert effective_min_soc(bat) == 20.0

    def test_at_threshold(self):
        bat = _bat(cell_temp_c=4.0)
        assert effective_min_soc(bat) == 15.0  # At threshold = normal

    def test_just_below_threshold(self):
        bat = _bat(cell_temp_c=3.9)
        assert effective_min_soc(bat) == 20.0


class TestAvailableKwh:
    def test_normal(self):
        bat = _bat(soc=50, cap_kwh=15.0)
        assert abs(available_kwh(bat) - 5.25) < 0.01  # (50-15)/100*15

    def test_at_min_soc(self):
        bat = _bat(soc=15, cap_kwh=15.0)
        assert available_kwh(bat) == 0.0

    def test_below_min_soc(self):
        bat = _bat(soc=10, cap_kwh=15.0)
        assert available_kwh(bat) == 0.0

    def test_cold_higher_min(self):
        bat = _bat(soc=18, cap_kwh=15.0, cell_temp_c=3.0)
        # effective_min = 20% → available = 0 (18 < 20)
        assert available_kwh(bat) == 0.0


class TestProportionalDischarge:
    def test_75_25_split(self):
        """Kontor 15kWh, Förråd 5kWh, same SoC → 75/25 split."""
        bats = [
            _bat("kontor", soc=97, cap_kwh=15.0),
            _bat("forrad", soc=97, cap_kwh=5.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 1500
        assert f.watts == 500
        assert result.total_w == 2000

    def test_different_soc_proportional(self):
        """Different SoC → proportional to available kWh."""
        bats = [
            _bat("kontor", soc=50, cap_kwh=15.0),  # avail = 5.25 kWh
            _bat("forrad", soc=90, cap_kwh=5.0),    # avail = 3.75 kWh
        ]
        result = calculate_proportional_discharge(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor share = 5.25 / 9.0 = 58.3%
        assert k.watts > f.watts
        assert abs(k.watts + f.watts - 2000) <= 1

    def test_one_at_min_soc(self):
        """One battery at min_soc → all to the other."""
        bats = [
            _bat("kontor", soc=50, cap_kwh=15.0),
            _bat("forrad", soc=15, cap_kwh=5.0),  # At min
        ]
        result = calculate_proportional_discharge(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 2000
        assert f.watts == 0
        assert f.at_min_soc is True

    def test_both_at_min_soc(self):
        """Both at min_soc → no discharge."""
        bats = [
            _bat("kontor", soc=15, cap_kwh=15.0),
            _bat("forrad", soc=14, cap_kwh=5.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        assert result.total_w == 0
        assert all(a.watts == 0 for a in result.allocations)

    def test_cold_lock_higher_min(self):
        """Cold battery → min_soc=20%, less available."""
        bats = [
            _bat("kontor", soc=25, cap_kwh=15.0, cell_temp_c=3.0),  # cold → min=20%
            _bat("forrad", soc=25, cap_kwh=5.0, cell_temp_c=15.0),  # warm → min=15%
        ]
        result = calculate_proportional_discharge(bats, 1000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor: (25-20)/100*15 = 0.75 kWh
        # forrad: (25-15)/100*5 = 0.5 kWh
        assert k.watts > f.watts  # kontor has more available

    def test_cold_battery_at_cold_min(self):
        """Cold battery at 18% → below cold min 20% → zero."""
        bats = [
            _bat("kontor", soc=18, cap_kwh=15.0, cell_temp_c=3.0),
            _bat("forrad", soc=50, cap_kwh=5.0, cell_temp_c=15.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0
        assert f.watts == 2000

    def test_max_discharge_per_battery(self):
        """Respect per-battery max discharge limit."""
        bats = [
            _bat("kontor", soc=97, cap_kwh=15.0, max_discharge_w=1000),
            _bat("forrad", soc=97, cap_kwh=5.0, max_discharge_w=500),
        ]
        result = calculate_proportional_discharge(bats, 5000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts <= 1000
        assert f.watts <= 500

    def test_convergence_check(self):
        """Same SoC + proportional → balanced (reach min_soc together)."""
        bats = [
            _bat("kontor", soc=50, cap_kwh=15.0),
            _bat("forrad", soc=50, cap_kwh=5.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        assert result.balanced is True

    def test_zero_watts(self):
        """Zero discharge → all allocations zero."""
        bats = [_bat("kontor", soc=50)]
        result = calculate_proportional_discharge(bats, 0)
        assert result.total_w == 0

    def test_empty_batteries_list(self):
        result = calculate_proportional_discharge([], 2000)
        assert result.total_w == 0

    def test_single_battery(self):
        bats = [_bat("kontor", soc=50, cap_kwh=15.0)]
        result = calculate_proportional_discharge(bats, 2000)
        assert result.allocations[0].watts == 2000


class TestProportionalCharge:
    def test_charge_emptiest_first(self):
        """Emptier battery gets more charge power."""
        bats = [
            _bat("kontor", soc=30, cap_kwh=15.0),   # room = 10.5 kWh
            _bat("forrad", soc=80, cap_kwh=5.0),     # room = 1.0 kWh
        ]
        result = calculate_proportional_charge(bats, 3000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts > f.watts

    def test_cold_battery_no_charge(self):
        """Cold battery can't charge → all to warm battery."""
        bats = [
            _bat("kontor", soc=30, cap_kwh=15.0, cell_temp_c=3.0),
            _bat("forrad", soc=30, cap_kwh=5.0, cell_temp_c=15.0),
        ]
        result = calculate_proportional_charge(bats, 3000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0  # Cold → can't charge
        assert f.watts == 3000

    def test_full_battery_no_charge(self):
        """Full battery gets 0."""
        bats = [
            _bat("kontor", soc=100, cap_kwh=15.0),
            _bat("forrad", soc=50, cap_kwh=5.0),
        ]
        result = calculate_proportional_charge(bats, 3000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0
        assert f.watts == 3000
