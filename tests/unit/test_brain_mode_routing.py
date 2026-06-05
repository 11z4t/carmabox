"""Tests for Brain Phase 1 mode-routing (_bat_mode_routing).

Feed-forward v5 (Borje 2026-05-22 16:20): AUTO = bat_actual_HW + grid_5min_avg.
  new_offer = bat_actual + grid_5min
  Deadband: |grid_5min| < 100W → hold current_offer (no change)
  Clamp: [-10000, +10000]

T1: mode=AUTO, grid_5min=+500W, bat_actual=200W -> offer=700W, source=AUTO
T1b: mode=AUTO, surplus (grid_5min=-640W, bat_actual=0W) -> offer=-640W (charge)
T2: mode=MANUAL, target=5000 -> offer=5000, source=MANUAL, computed NOT used
T3: mode=MANUAL, target=-5000 -> offer=-5000 (discharge)
T4: mode=MANUAL, target=unavailable -> offer=0 (fail-safe)
T5: mode=SHADOW -> offer=0, source=SHADOW
T6: mode=unknown -> AUTO safe-default (feed-forward)
T7: mode helper entity missing (None) -> AUTO safe-default (feed-forward)
T6b: mode=unavailable -> AUTO safe-default (feed-forward)
T8: AUTO deadband: |grid_5min| < 100W -> hold current_offer
T9: AUTO grid_5min_avg unavailable -> fallback 0.0
T10: AUTO clamp: large grid_5min + bat -> clamped to 10000W
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.brain.brain import BrainController


def _controller(
    mode: str | None,
    target_manual: str | None = None,
    grid_5min: str | None = None,
    bat_kontor: str | None = None,
    bat_forrad: str | None = None,
    current_offer: str | None = None,
    bat_soc: str = "80.0",
    bat_floor: str | None = None,
) -> BrainController:
    """Create a BrainController with mocked hass for mode-routing tests."""
    hass = MagicMock()

    def fake_states_get(entity_id: str):
        mapping = {
            "input_select.bat_balancer_mode": mode,
            "input_number.bat_balancer_target_manual_w": target_manual,
            "sensor.brain_grid_w_5min_avg": grid_5min,
            "sensor.goodwe_battery_power_kontor": bat_kontor,
            "sensor.goodwe_battery_power_forrad": bat_forrad,
            "input_number.brain_target_bat_w": current_offer,
            "sensor.bat_balancer_avg_soc_pct": bat_soc,
            "input_number.brain_bat_floor_day_pct": bat_floor,
        }
        val = mapping.get(entity_id)
        if entity_id not in mapping:
            return None
        if val is None:
            return None
        s = MagicMock()
        s.state = val
        return s

    hass.states.get = MagicMock(side_effect=fake_states_get)
    ctrl = BrainController.__new__(BrainController)
    ctrl._hass = hass
    ctrl._last_bat_offer_source = None
    return ctrl


class TestAutoMode:
    def test_t1_auto_feed_forward_import(self) -> None:
        """T1: AUTO, grid importing 500W, bat_actual=200W -> offer=700W."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="500.0",
            bat_kontor="150.0",
            bat_forrad="50.0",
            current_offer="0",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 700.0  # 200 + 500
        assert source == "AUTO"

    def test_t1d_auto_floor_blocked_discharge_returns_zero(self) -> None:
        """T1d: AUTO, grid importing, but SoC at floor → offer=0W (floor guard)."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="500.0",
            bat_kontor="0.0",
            bat_forrad="0.0",
            current_offer="0",
            bat_soc="15.0",  # at floor
            bat_floor="50.0",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 0.0  # floor guard: soc=15 <= floor=50
        assert source == "AUTO"

    def test_t1b_auto_feed_forward_surplus(self) -> None:
        """T1b: AUTO, PV surplus grid_5min=-640W, bat idle -> offer=-640W (charge)."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="-640.0",
            bat_kontor="0.0",
            bat_forrad="0.0",
            current_offer="0",
            bat_soc="100.0",  # at ceiling: no 0-vision enforcement
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == -640.0
        assert source == "AUTO"

    def test_t1c_auto_feed_forward_discharging(self) -> None:
        """T1c: AUTO, bat discharging 800W (pos in goodwe convention), grid 300W -> offer=1100W."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="300.0",
            bat_kontor="600.0",
            bat_forrad="200.0",
            current_offer="800",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 1100.0  # 800 + 300
        assert source == "AUTO"

    def test_t8_auto_deadband_holds_offer(self) -> None:
        """T8: |grid_5min| < 100W -> deadband hold: return current_offer unchanged."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="50.0",
            bat_kontor="200.0",
            bat_forrad="100.0",
            current_offer="800",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 800.0  # held — deadband
        assert source == "AUTO"

    def test_t8b_auto_deadband_negative_holds(self) -> None:
        """T8b: negative grid_5min within deadband (-80W) -> hold."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="-80.0",
            bat_kontor="0.0",
            bat_forrad="0.0",
            current_offer="-500",
        )
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == -500.0

    def test_t9_auto_grid_5min_unavailable_returns_zero(self) -> None:
        """T9: grid_5min_avg sensor unavailable -> fallback 0.0."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min=None,  # unavailable
            bat_kontor="200.0",
            bat_forrad="100.0",
            current_offer="500",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 0.0
        assert source == "AUTO"

    def test_t10_auto_clamp_max(self) -> None:
        """T10: huge grid_5min + bat_actual -> clamped to +10000W."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="8000.0",
            bat_kontor="3000.0",
            bat_forrad="2000.0",
            current_offer="0",
        )
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 10000.0  # clamped

    def test_t10b_auto_clamp_min(self) -> None:
        """T10b: large surplus -> clamped to -10000W."""
        ctrl = _controller(
            mode="AUTO",
            grid_5min="-9000.0",
            bat_kontor="-2000.0",
            bat_forrad="-1000.0",
            current_offer="0",
            bat_soc="100.0",  # at ceiling: no 0-vision enforcement
        )
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == -10000.0


class TestManualMode:
    def test_t2_manual_returns_target_not_computed(self) -> None:
        """T2: mode=MANUAL, target=5000 -> offer=5000, NOT computed_bat_w."""
        ctrl = _controller(mode="MANUAL", target_manual="5000")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-9999.0)
        assert offer_w == 5000.0
        assert source == "MANUAL"

    def test_t3_manual_preserves_negative_sign(self) -> None:
        """T3: mode=MANUAL, target=-5000 -> offer=-5000 (discharge)."""
        ctrl = _controller(mode="MANUAL", target_manual="-5000")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == -5000.0
        assert source == "MANUAL"

    def test_t4_manual_unavailable_target_returns_zero(self) -> None:
        """T4: mode=MANUAL, target=unavailable -> fail-safe offer=0."""
        ctrl = _controller(mode="MANUAL", target_manual="unavailable")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-3000.0)
        assert offer_w == 0.0
        assert source == "MANUAL"

    def test_t4b_manual_missing_entity_returns_zero(self) -> None:
        """T4b: mode=MANUAL, target entity missing (None) -> fail-safe offer=0."""
        ctrl = _controller(mode="MANUAL", target_manual=None)
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-3000.0)
        assert offer_w == 0.0
        assert source == "MANUAL"

    def test_manual_ignores_grid_sensors(self) -> None:
        """MANUAL never uses grid_5min_avg regardless of its value."""
        ctrl = _controller(mode="MANUAL", target_manual="3000", grid_5min="9000.0")
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=-9999.0)
        assert offer_w == 3000.0  # NOT grid-influenced


class TestShadowMode:
    def test_t5_shadow_returns_zero(self) -> None:
        """T5: mode=SHADOW -> offer=0, source=SHADOW."""
        ctrl = _controller(mode="SHADOW")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-5000.0)
        assert offer_w == 0.0
        assert source == "SHADOW"

    def test_t5_shadow_ignores_computed(self) -> None:
        """T5b: SHADOW ignores any positive computed value too."""
        ctrl = _controller(mode="SHADOW")
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=4000.0)
        assert offer_w == 0.0

    def test_shadow_ignores_grid_sensors(self) -> None:
        """SHADOW never uses grid_5min_avg regardless of its value."""
        ctrl = _controller(mode="SHADOW", grid_5min="9000.0", bat_kontor="3000.0")
        offer_w, _ = ctrl._bat_mode_routing(computed_bat_w=0.0)
        assert offer_w == 0.0


class TestSafeDefaults:
    def test_t6_unknown_mode_treated_as_auto(self) -> None:
        """T6: mode=unknown -> AUTO safe-default -> feed-forward."""
        ctrl = _controller(
            mode="unknown",
            grid_5min="400.0",
            bat_kontor="100.0",
            bat_forrad="50.0",
            current_offer="0",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-1500.0)
        assert offer_w == 550.0  # 150 + 400
        assert source == "AUTO"

    def test_t7_missing_mode_entity_treated_as_auto(self) -> None:
        """T7: mode entity missing (None) -> AUTO safe-default -> feed-forward."""
        ctrl = _controller(
            mode=None,
            grid_5min="300.0",
            bat_kontor="0.0",
            bat_forrad="0.0",
            current_offer="0",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-1500.0)
        assert offer_w == 300.0  # 0 + 300
        assert source == "AUTO"

    def test_t6b_unavailable_mode_treated_as_auto(self) -> None:
        """T6b: mode=unavailable (HA restart) -> AUTO safe-default -> feed-forward."""
        ctrl = _controller(
            mode="unavailable",
            grid_5min="500.0",
            bat_kontor="1000.0",
            bat_forrad="500.0",
            current_offer="0",
        )
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=2000.0)
        assert offer_w == 2000.0  # 1500 + 500
        assert source == "AUTO"
