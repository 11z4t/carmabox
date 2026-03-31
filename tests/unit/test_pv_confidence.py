"""EXP-13: Tempest PV confidence calculation tests."""

from __future__ import annotations

from custom_components.carmabox.core.planner import calculate_pv_confidence


class TestPVConfidence:
    """Tempest pressure + radiation -> PV confidence multiplier."""

    def test_night_always_1(self) -> None:
        """Night hours -> always 1.0 regardless of weather."""
        assert calculate_pv_confidence(1040, 0, 5.0, hour=3) == 1.0
        assert calculate_pv_confidence(980, 0, 5.0, hour=22) == 1.0

    def test_high_pressure_high_confidence(self) -> None:
        """High pressure (1030+) -> confidence > 1.0."""
        conf = calculate_pv_confidence(1035, 800, 4.0, hour=12)
        assert conf >= 1.0

    def test_low_pressure_low_confidence(self) -> None:
        """Low pressure (< 1005) -> confidence < 1.0."""
        conf = calculate_pv_confidence(995, 200, 4.0, hour=12)
        assert conf < 1.0

    def test_normal_pressure_near_1(self) -> None:
        """Normal pressure (1013) -> confidence near 1.0."""
        conf = calculate_pv_confidence(1013, 800, 4.0, hour=12)
        assert 0.8 <= conf <= 1.15

    def test_radiation_validates_solcast(self) -> None:
        """Low radiation vs high Solcast -> reduced confidence."""
        # Solcast says 5kW but radiation only 100 W/m2 (should be ~1000)
        conf_low = calculate_pv_confidence(1013, 100, 5.0, hour=12)
        # Solcast says 5kW and radiation confirms at 900 W/m2
        conf_high = calculate_pv_confidence(1013, 900, 5.0, hour=12)
        assert conf_low < conf_high

    def test_no_solcast_estimate_pressure_only(self) -> None:
        """No Solcast estimate -> use pressure only."""
        conf = calculate_pv_confidence(1035, 500, 0.0, hour=10)
        assert conf >= 1.0  # High pressure = good

    def test_clamp_max_1_2(self) -> None:
        """Confidence never exceeds 1.2."""
        conf = calculate_pv_confidence(1050, 1500, 2.0, hour=12)
        assert conf <= 1.2

    def test_clamp_min_0_5(self) -> None:
        """Confidence never below 0.5."""
        conf = calculate_pv_confidence(950, 0, 10.0, hour=12)
        assert conf >= 0.5

    def test_early_morning_pressure_only(self) -> None:
        """Hour 7: radiation may be low naturally -> pressure dominates."""
        conf = calculate_pv_confidence(1025, 50, 0.3, hour=7)
        assert conf >= 0.7

    def test_returns_float_rounded(self) -> None:
        """Result should be rounded to 2 decimals."""
        conf = calculate_pv_confidence(1015, 500, 3.0, hour=12)
        assert conf == round(conf, 2)
