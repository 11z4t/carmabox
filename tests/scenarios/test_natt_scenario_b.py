"""Scenario B — Solig vårdag.

State: SoC K=97% F=96%, sol imorgon=38 kWh, bat temp K=5.9°C
Expected:
  - Planner generates discharge plan (high SoC + good solar tomorrow)
  - Batteries drain toward min_soc before sunrise
  - Solar-aware floor = min_soc (good PV tomorrow)
  - Cold temperature does NOT block discharge (5.9°C > 4°C min)
"""

from __future__ import annotations

from custom_components.carmabox.optimizer.planner import generate_plan
from custom_components.carmabox.optimizer.safety_guard import SafetyGuard


class TestScenarioB:
    """Scenario B: Solig vårdag — high SoC, good solar forecast."""

    def _spring_plan(self) -> list:
        """Generate plan for sunny spring day starting at 22:00."""
        n = 32  # 22:00 → 06:00 next day + rest of day
        # Prices: cheap night, expensive morning
        prices = [30] * 8 + [90, 100, 110, 120, 100, 80, 60, 50] + [40] * 16
        # PV: zero at night, strong day (38 kWh total)
        pv = [0] * 8 + [0.5, 2, 4, 6, 7, 7, 6, 5, 4, 2, 0.5, 0] + [0] * 12
        loads = [1.5] * n
        return generate_plan(
            num_hours=n,
            start_hour=22,
            target_weighted_kw=2.0,
            hourly_loads=loads,
            hourly_pv=pv[:n],
            hourly_prices=prices[:n],
            hourly_ev=[0.0] * n,
            battery_soc=96.5,  # Weighted avg of K=97%, F=96%
            ev_soc=-1.0,
            battery_cap_kwh=20.0,
            battery_min_soc=15.0,
            night_weight=0.5,
        )

    def test_discharge_planned_with_high_soc(self) -> None:
        """High SoC + good solar → discharge plan generated."""
        plan = self._spring_plan()
        discharge_hours = [h for h in plan if h.action == "d"]
        assert len(discharge_hours) > 0, "Should discharge with 96.5% SoC"

    def test_solar_floor_is_min_soc(self) -> None:
        """Good solar tomorrow (38kWh) → floor = min_soc only."""
        plan = self._spring_plan()
        # With 38 kWh PV tomorrow, floor should be min_soc (15%)
        # Battery should drain well below 50%
        min_soc_in_plan = min(h.battery_soc for h in plan)
        assert (
            min_soc_in_plan <= 30
        ), f"With 38kWh solar tomorrow, battery should drain below 30%, got {min_soc_in_plan}%"

    def test_charge_hours_during_solar(self) -> None:
        """PV surplus hours should show charge action."""
        plan = self._spring_plan()
        charge_hours = [h for h in plan if h.action == "c"]
        assert len(charge_hours) > 0, "Should charge from PV during sunny hours"

    def test_cold_temp_does_not_block(self) -> None:
        """5.9°C is above SafetyGuard min discharge temp (4°C default)."""
        guard = SafetyGuard()
        result = guard.check_discharge(
            soc_1=97.0,
            soc_2=96.0,
            min_soc=15.0,
            grid_power_w=500.0,
            temp_c=5.9,
        )
        assert result.ok is True, f"5.9°C should allow discharge, got: {result.reason}"
