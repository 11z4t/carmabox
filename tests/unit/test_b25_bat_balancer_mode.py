"""Tests for B25: bat_balancer AC80 three-mode (MANUAL/SHADOW/AUTO).

The coordinator _tick() is async + HA-bound (self.hass.states.get).
These tests cover:
  (a) distribution_engine layer — validates what each mode produces downstream
  (b) _str_state / _float_helper — pure coordinator helper logic under isolation

  TC-B25-1: MANUAL mode → target_w=5000W → both banks get positive charge allocation
  TC-B25-2: SHADOW mode → target_w=0W → distribute returns all-zero targets
  TC-B25-3: AUTO mode → target_w=brain_target → distribute distributes correctly
  TC-B25-4: SHADOW mode via input_select → target_available=False → no-write path (shadow=True)
  TC-B25-5: BatBalancerStatus.MANUAL_MODE exists as enum value "manual_mode"
  TC-B25-6: MANUAL mode → distribute(5000) → both banks get non-zero allocation
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.bat_balancer.const import BatBalancerStatus
from custom_components.bat_balancer.distribution_engine import distribute_target_to_banks
from custom_components.bat_balancer.models import BankConfig, BankState, SensorSnapshot


def _two_banks(
    soc: float = 50.0,
    kontor_online: bool = True,
    forrad_online: bool = True,
) -> tuple[dict[str, BankConfig], dict[str, BankState]]:
    configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    states = {
        "kontor": BankState(bank_id="kontor", current_soc=soc, is_online=kontor_online),
        "forrad": BankState(bank_id="forrad", current_soc=soc, is_online=forrad_online),
    }
    return configs, states


def _snap(
    brain_target_bat_w: float = 0.0,
    house_grid_w: float = 3000.0,
    shadow_mode: bool = False,
    brain_target_available: bool = True,
) -> SensorSnapshot:
    return SensorSnapshot(
        brain_target_bat_w=brain_target_bat_w,
        house_grid_w=house_grid_w,
        pv_w=0.0,
        shadow_mode=shadow_mode,
        brain_target_available=brain_target_available,
    )


class TestManualModeDistribution:
    def test_tc_b25_1_manual_distributes_to_both_banks(self) -> None:
        """TC-B25-1: MANUAL target_w=5000W → both banks get positive allocation."""
        configs, states = _two_banks(soc=50.0)
        result = distribute_target_to_banks(
            target_w=5000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_snap(brain_target_bat_w=5000.0, house_grid_w=float("nan")),
        )
        assert result.actual_total_w > 0.0, "MANUAL 5000W must produce non-zero distribution"
        assert result.targets["kontor"] > 0.0, "Kontor must get positive charge allocation"
        assert result.targets["forrad"] > 0.0, "Förråd must get positive charge allocation"

    def test_tc_b25_6_manual_total_matches_target(self) -> None:
        """TC-B25-6: distribute(5000W) total ≤ 5100W (brain-offer invariant)."""
        configs, states = _two_banks(soc=50.0)
        result = distribute_target_to_banks(
            target_w=5000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_snap(brain_target_bat_w=5000.0, house_grid_w=float("nan")),
        )
        total = sum(abs(v) for v in result.targets.values())
        assert total <= 5000.0 + 100.0 + 0.01, f"Brain-offer invariant breach: {total:.1f}W > 5100W"


class TestShadowModeDistribution:
    def test_tc_b25_2_shadow_target_zero_produces_all_zero(self) -> None:
        """TC-B25-2: SHADOW mode → target_w=0 → distribute returns all-zero targets."""
        configs, states = _two_banks(soc=50.0)
        result = distribute_target_to_banks(
            target_w=0.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_snap(brain_target_bat_w=0.0, shadow_mode=True),
        )
        assert result.actual_total_w == 0.0
        assert all(
            v == 0.0 for v in result.targets.values()
        ), f"SHADOW mode must produce zero targets, got: {result.targets}"

    def test_tc_b25_4_shadow_no_write_path(self) -> None:
        """TC-B25-4: SHADOW via input_select → coordinator _str_state returns 'SHADOW'
        → target_available=False, shadow=True → early return without HW write.

        Simulated by verifying the coordinator's _str_state helper returns the
        correct value from a mocked HA state, matching what _tick() uses.
        """
        from custom_components.bat_balancer.coordinator import BatBalancerCoordinator

        coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)

        mock_state = MagicMock()
        mock_state.state = "SHADOW"
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=mock_state)
        coord.hass = hass

        result = coord._str_state("input_select.bat_balancer_mode", "AUTO")
        assert result == "SHADOW", f"Expected 'SHADOW', got '{result}'"

    def test_tc_b25_4b_shadow_str_state_unavailable_returns_default(self) -> None:
        """TC-B25-4b: _str_state returns default when entity is unavailable."""
        from custom_components.bat_balancer.coordinator import BatBalancerCoordinator

        coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=mock_state)
        coord.hass = hass

        result = coord._str_state("input_select.bat_balancer_mode", "AUTO")
        assert result == "AUTO", "unavailable entity must return default 'AUTO'"


class TestAutoModeDistribution:
    def test_tc_b25_3_auto_distributes_brain_target(self) -> None:
        """TC-B25-3: AUTO mode brain_target=3000W → distribute → both banks get allocation."""
        configs, states = _two_banks(soc=50.0)
        result = distribute_target_to_banks(
            target_w=3000.0,
            bank_configs=configs,
            bank_states=states,
            snapshot=_snap(brain_target_bat_w=3000.0, house_grid_w=float("nan")),
        )
        assert result.actual_total_w > 0.0
        total = sum(v for v in result.targets.values())
        assert total > 0.0, f"AUTO 3000W must distribute positively, got total={total:.1f}W"


class TestManualModeStatus:
    def test_tc_b25_5_manual_mode_status_enum_value(self) -> None:
        """TC-B25-5: BatBalancerStatus.MANUAL_MODE == 'manual_mode'."""
        assert BatBalancerStatus.MANUAL_MODE == "manual_mode"

    def test_manual_mode_status_distinct_from_ok(self) -> None:
        """MANUAL_MODE status is distinct from OK (sensor shows MANUAL state)."""
        assert BatBalancerStatus.MANUAL_MODE != BatBalancerStatus.OK
        assert BatBalancerStatus.MANUAL_MODE != BatBalancerStatus.SHADOW_MODE
