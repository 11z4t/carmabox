"""Coverage tests for remaining small gaps — batch 22.

Targets:
  core/planner.py:        229 (re-test: battery full + low headroom → else branch)
  core/ml_predictor.py:   170, 189, 219, 234
  sensor.py:              461
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ══════════════════════════════════════════════════════════════════════════════
# core/planner.py
# ══════════════════════════════════════════════════════════════════════════════


class TestPlannerBatch22:
    """Line 229: battery full + will_export=True but max_ev_kw < ev_1phase_kw."""

    def test_plan_solar_allocation_battery_full_low_headroom(self) -> None:
        """Battery=100% + high consumption → max_ev_kw < 1-phase threshold → line 229.

        avg_pv=1.505, avg_consumption=2.5 → max_ev_kw=1.005 < ev_1phase_kw=1.38
        """
        from custom_components.carmabox.core.planner import plan_solar_allocation

        result = plan_solar_allocation(
            battery_soc_pct=100.0,  # Full battery → Rule 4: will_export = total_surplus > 0
            battery_cap_kwh=15.0,
            ev_soc_pct=50.0,
            ev_target_pct=80.0,
            ev_cap_kwh=60.0,
            hourly_pv_kw=[3.0, 0.01],  # Hour 0: big PV → surplus; hour 1: tiny PV
            hourly_consumption_kw=[2.0, 3.0],  # Mix keeps avg_consumption high
            current_hour=10,
            sunset_hour=12,  # hours_left=2 → n=2
        )
        assert result.ev_can_charge is False
        assert result.ev_recommended_amps == 0


# ══════════════════════════════════════════════════════════════════════════════
# core/ml_predictor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestMLPredictorBatch22:
    """Lines 170 (pressure_pv trim), 189 (outcomes trim), 219 (no ratios), 234 (neutral p)."""

    def _make_predictor(self):
        from custom_components.carmabox.core.ml_predictor import MLPredictor

        return MLPredictor()

    def test_add_pressure_pv_trims_to_100(self) -> None:
        """Adding 101 entries triggers the [-100:] slice on line 170."""
        pred = self._make_predictor()
        for i in range(101):
            pred.add_pressure_pv(pressure_hpa=1013.0 + i * 0.01, pv_ratio=0.9)
        # Internal list trimmed to 100
        assert len(pred._pressure_pv) == 100

    def test_add_decision_outcome_trims_to_200(self) -> None:
        """Adding 201 entries triggers the [-200:] slice on line 189."""
        pred = self._make_predictor()
        for i in range(201):
            pred.add_decision_outcome(
                decision="discharge",
                context={"hour": i % 24},
                outcome="ok",
                laws_ok=True,
            )
        assert len(pred._decision_outcomes) == 200

    def test_get_plan_correction_factor_no_valid_ratios(self) -> None:
        """All samples have planned_grid_kw <= 0.1 → ratios list empty → line 219 return 1.0."""
        from custom_components.carmabox.core.ml_predictor import PlanAccuracySample

        pred = self._make_predictor()
        hour = 8
        for _ in range(3):
            pred.add_plan_accuracy(
                PlanAccuracySample(
                    hour=hour,
                    planned_grid_kw=0.05,  # <= 0.1 → filtered out of ratios
                    actual_grid_kw=0.1,
                    planned_action="standby",
                    actual_action="standby",
                    price=50.0,
                )
            )
        result = pred.get_plan_correction_factor(hour)
        assert result == 1.0

    def test_predict_pv_correction_neutral_pressure(self) -> None:
        """Pressure 1010 hPa (1005-1015 range) → neither high nor low → line 234 return 1.0."""
        pred = self._make_predictor()
        # Add some high- and low-pressure data so _pressure_pv is not empty
        pred.add_pressure_pv(pressure_hpa=1020.0, pv_ratio=1.1)
        pred.add_pressure_pv(pressure_hpa=1000.0, pv_ratio=0.8)
        # Neutral pressure: not > 1015 and not < 1005
        result = pred.predict_pv_correction(pressure_hpa=1010.0)
        assert result == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# sensor.py
# ══════════════════════════════════════════════════════════════════════════════


class TestSensorBatch22:
    """Line 461: _daily_insight_attrs returns coord.daily_insight."""

    def test_daily_insight_attrs_returns_coord_value(self) -> None:
        """_daily_insight_attrs(coord) → coord.daily_insight (line 461)."""
        from custom_components.carmabox.sensor import _daily_insight_attrs

        mock_coord = MagicMock()
        mock_coord.daily_insight = {"max_kw": 2.5, "total_cost_kr": 45.0}
        result = _daily_insight_attrs(mock_coord)
        assert result == {"max_kw": 2.5, "total_cost_kr": 45.0}
