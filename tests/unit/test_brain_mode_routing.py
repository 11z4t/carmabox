"""Tests for Brain Phase 1 mode-routing (_bat_mode_routing).

T1: mode=AUTO -> grid_null-computed value returned, offer_source=AUTO
T2: mode=MANUAL, target=5000 -> offer=5000, offer_source=MANUAL, computed NOT used
T3: mode=MANUAL, target=-5000 -> offer=-5000 (sign preserved)
T4: mode=MANUAL, target=unavailable -> offer=0 (fail-safe)
T5: mode=SHADOW -> offer=0, offer_source=SHADOW
T6: mode=unknown -> safe-default AUTO behaviour (returns computed_bat_w)
T7: mode helper entity missing (None) -> safe-default AUTO
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.brain.brain import BrainController


def _controller(mode: str | None, target_manual: str | None = None) -> BrainController:
    """Create a BrainController with mocked hass that returns the given mode state."""
    hass = MagicMock()

    def fake_states_get(entity_id: str):
        if entity_id == "input_select.bat_balancer_mode":
            if mode is None:
                return None
            s = MagicMock()
            s.state = mode
            return s
        if entity_id == "input_number.bat_balancer_target_manual_w":
            if target_manual is None:
                return None
            s = MagicMock()
            s.state = target_manual
            return s
        return None

    hass.states.get = MagicMock(side_effect=fake_states_get)
    ctrl = BrainController.__new__(BrainController)
    ctrl._hass = hass
    return ctrl


class TestAutoMode:
    def test_t1_auto_returns_computed(self) -> None:
        """T1: mode=AUTO -> cascade-computed value, source=AUTO."""
        ctrl = _controller(mode="AUTO")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-2500.0)
        assert offer_w == -2500.0
        assert source == "AUTO"

    def test_t1_auto_does_not_alter_sign(self) -> None:
        """T1b: Positive computed (charge) also passed through unchanged."""
        ctrl = _controller(mode="AUTO")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=3000.0)
        assert offer_w == 3000.0
        assert source == "AUTO"


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


class TestSafeDefaults:
    def test_t6_unknown_mode_treated_as_auto(self) -> None:
        """T6: mode=unknown -> AUTO safe-default -> returns computed_bat_w."""
        ctrl = _controller(mode="unknown")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-1500.0)
        assert offer_w == -1500.0
        assert source == "AUTO"

    def test_t7_missing_mode_entity_treated_as_auto(self) -> None:
        """T7: mode entity missing (None) -> AUTO safe-default -> returns computed_bat_w."""
        ctrl = _controller(mode=None)
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=-1500.0)
        assert offer_w == -1500.0
        assert source == "AUTO"

    def test_t6b_unavailable_mode_treated_as_auto(self) -> None:
        """T6b: mode=unavailable (HA restart) -> AUTO safe-default."""
        ctrl = _controller(mode="unavailable")
        offer_w, source = ctrl._bat_mode_routing(computed_bat_w=2000.0)
        assert offer_w == 2000.0
        assert source == "AUTO"
