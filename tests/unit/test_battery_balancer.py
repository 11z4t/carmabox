"""Tests for Battery Balancer — proportional discharge/charge."""

from __future__ import annotations

from custom_components.carmabox.core.battery_balancer import (
    BatteryInfo,
    available_kwh,
    calculate_proportional_charge,
    calculate_proportional_discharge,
    effective_min_soc,
    redistribute_on_depletion,
)


def _bat(
    consumer_id: str = "kontor",
    soc: float = 50,
    cap_kwh: float = 15.0,
    cell_temp_c: float = 15.0,
    min_soc: float = 15.0,
    min_soc_cold: float = 20.0,
    cold_temp_c: float = 4.0,
    max_discharge_w: float = 5000.0,
    soh_pct: float = 100.0,
) -> BatteryInfo:
    return BatteryInfo(
        id=consumer_id,
        soc=soc,
        cap_kwh=cap_kwh,
        cell_temp_c=cell_temp_c,
        min_soc=min_soc,
        min_soc_cold=min_soc_cold,
        cold_temp_c=cold_temp_c,
        max_discharge_w=max_discharge_w,
        soh_pct=soh_pct,
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
            _bat("forrad", soc=90, cap_kwh=5.0),  # avail = 3.75 kWh
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
        """Cold battery (3C) -> min_soc=20% + EXP-07 50% discharge derating."""
        bats = [
            _bat("kontor", soc=25, cap_kwh=15.0, cell_temp_c=3.0),  # cold: min=20%, 50% derating
            _bat("forrad", soc=25, cap_kwh=5.0, cell_temp_c=15.0),  # warm: min=15%
        ]
        result = calculate_proportional_discharge(bats, 1000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor: available but 50% derating at 3C (EXP-07)
        # forrad: warm, no derating
        assert k.watts > 0  # kontor still contributes
        assert f.watts > 0  # forrad contributes

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
            _bat("kontor", soc=30, cap_kwh=15.0),  # room = 10.5 kWh
            _bat("forrad", soc=80, cap_kwh=5.0),  # room = 1.0 kWh
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


class TestRedistributeOnDepletion:
    def test_redistribute_one_depleted(self):
        """bat1 at 15% (min_soc), bat2 at 50% → bat2 takes all."""
        bats = [
            _bat("kontor", soc=15.0, cap_kwh=15.0),
            _bat("forrad", soc=50.0, cap_kwh=5.0),
        ]
        result = redistribute_on_depletion(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0
        assert k.at_min_soc is True
        assert f.watts == 2000
        assert result.total_w == 2000

    def test_redistribute_both_available(self):
        """Both above min+margin → normal proportional split."""
        bats = [
            _bat("kontor", soc=50.0, cap_kwh=15.0),
            _bat("forrad", soc=50.0, cap_kwh=5.0),
        ]
        result = redistribute_on_depletion(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 1500  # 75% of 2000
        assert f.watts == 500  # 25% of 2000
        assert result.total_w == 2000

    def test_redistribute_all_depleted(self):
        """Both at min_soc → 0W for all."""
        bats = [
            _bat("kontor", soc=15.0, cap_kwh=15.0),
            _bat("forrad", soc=14.0, cap_kwh=5.0),
        ]
        result = redistribute_on_depletion(bats, 2000)
        assert result.total_w == 0
        assert all(a.watts == 0 for a in result.allocations)

    def test_redistribute_margin(self):
        """bat at 15.5% (within 1% margin above min_soc=15) → excluded."""
        bats = [
            _bat("kontor", soc=15.5, cap_kwh=15.0),  # <= 16.0 threshold → excluded
            _bat("forrad", soc=50.0, cap_kwh=5.0),
        ]
        result = redistribute_on_depletion(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0
        assert k.at_min_soc is True
        assert f.watts == 2000


class TestBMSCurrentLimits:
    """EXP-02: BMS discharge current limit caps allocation."""

    def test_bms_limit_caps_allocation(self) -> None:
        """If BMS allows only 2000W, battery gets max 2000W even if share is higher."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, max_discharge_w=2000),
            _bat("forrad", soc=80, cap_kwh=5.0, max_discharge_w=5000),
        ]
        result = calculate_proportional_discharge(bats, 5000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor share = 75% of 5000 = 3750, but BMS caps at 2000
        assert k.watts == 2000
        # forrad gets its proportional share (25% = 1250)
        assert f.watts == 1250

    def test_bms_zero_disables_battery(self) -> None:
        """BMS discharge limit 0A = battery cannot discharge."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, max_discharge_w=0),
            _bat("forrad", soc=80, cap_kwh=5.0, max_discharge_w=5000),
        ]
        result = calculate_proportional_discharge(bats, 3000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor capped to 0 by BMS
        assert k.watts == 0
        # forrad gets its share only
        assert f.watts == 750

    def test_both_bms_limited(self) -> None:
        """Both batteries BMS-limited — total discharge < requested."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, max_discharge_w=1000),
            _bat("forrad", soc=80, cap_kwh=5.0, max_discharge_w=500),
        ]
        result = calculate_proportional_discharge(bats, 5000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 1000
        assert f.watts == 500
        assert result.total_w == 1500  # Only 1500W possible out of 5000 requested

    def test_asymmetric_cold_bms_limit(self) -> None:
        """Cold kontor (BMS 2800W), warm forrad (8800W) — caps + EXP-07 derating."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, cell_temp_c=3.0, max_discharge_w=2800),
            _bat("forrad", soc=80, cap_kwh=5.0, cell_temp_c=15.0, max_discharge_w=8800),
        ]
        result = calculate_proportional_discharge(bats, 4000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        # kontor cold: min_soc=20%, available = (80-20)/100*15 = 9.0 kWh
        # forrad warm: min_soc=15%, available = (80-15)/100*5 = 3.25 kWh
        # kontor share = 9.0/12.25 = 73.5% of 4000 = 2938 → BMS cap 2800 → EXP-07 50% = 1400
        assert k.watts == 1400
        assert f.watts > 0


class TestSoHDerating:
    """EXP-06: SoH monitoring — aged batteries get higher min_soc."""

    def test_soh_100_no_derating(self):
        """Healthy battery (100% SoH) — no derating applied."""
        bat = _bat(soh_pct=100.0, min_soc=15.0)
        assert effective_min_soc(bat) == 15.0

    def test_soh_75_adds_5_pct(self):
        """SoH 75% (< 80%) — adds 5% to min_soc."""
        bat = _bat(soh_pct=75.0, min_soc=15.0)
        assert effective_min_soc(bat) == 20.0

    def test_soh_65_adds_10_pct(self):
        """SoH 65% (< 70%) — adds 10% to min_soc."""
        bat = _bat(soh_pct=65.0, min_soc=15.0)
        assert effective_min_soc(bat) == 25.0

    def test_soh_and_cold_combine(self):
        """Cold + degraded battery — both deratings are cumulative."""
        bat = _bat(
            soh_pct=75.0,
            cell_temp_c=3.0,
            min_soc=15.0,
            min_soc_cold=20.0,
        )
        # Cold: base = min_soc_cold = 20%, SoH < 80%: +5% → 25%
        assert effective_min_soc(bat) == 25.0


class TestColdDischargeBlock:
    """EXP-07: Cold-lock discharge blocking for LFP safety."""

    def test_below_zero_blocks_discharge(self):
        """Battery below 0C gets 0W discharge (LFP safety)."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, cell_temp_c=-5.0),
            _bat("forrad", soc=80, cap_kwh=5.0, cell_temp_c=15.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        k = next(a for a in result.allocations if a.id == "kontor")
        f = next(a for a in result.allocations if a.id == "forrad")
        assert k.watts == 0  # Blocked by cold-lock
        assert f.watts > 0

    def test_between_0_and_4_reduces_50pct(self):
        """Battery between 0-4C gets 50% discharge reduction."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, cell_temp_c=2.0),
        ]
        result_cold = calculate_proportional_discharge(bats, 2000)
        k_cold = result_cold.allocations[0]

        bats_warm = [
            _bat("kontor", soc=80, cap_kwh=15.0, cell_temp_c=15.0),
        ]
        result_warm = calculate_proportional_discharge(bats_warm, 2000)
        k_warm = result_warm.allocations[0]

        # Cold battery should get ~50% of what warm battery gets
        assert k_cold.watts == int(k_warm.watts * 0.5)

    def test_above_4_no_derating(self):
        """Battery above 4C gets full discharge (no cold derating)."""
        bats = [
            _bat("kontor", soc=80, cap_kwh=15.0, cell_temp_c=5.0),
        ]
        result = calculate_proportional_discharge(bats, 2000)
        assert result.allocations[0].watts == 2000
