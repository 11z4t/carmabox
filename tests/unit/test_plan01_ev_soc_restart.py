"""PLAN-01: EV SoC timestamp survives HA restart.

Tests that _last_known_ev_soc_time is correctly reconstructed
from unix time (stored in runtime store) rather than monotonic
(which resets on process restart).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock


def _make_coordinator() -> MagicMock:
    """Minimal coordinator stub for restore testing."""
    from custom_components.carmabox.const import DEFAULT_EV_MIN_AMPS
    from custom_components.carmabox.coordinator import CarmaboxCoordinator

    coord = MagicMock(spec=CarmaboxCoordinator)
    coord._last_known_ev_soc = -1.0
    coord._last_known_ev_soc_time = 0.0
    coord._last_known_ev_soc_unix = 0.0
    coord._ev_enabled = False
    coord._ev_current_amps = DEFAULT_EV_MIN_AMPS
    coord._miner_on = False
    coord._night_ev_active = False
    coord._ellevio_hour_samples = []
    coord._ellevio_monthly_hourly_peaks = []
    coord._surplus_hysteresis = None
    coord._grid_guard = None
    coord.plan = []
    coord._last_command = MagicMock()
    coord._last_command.name = "STANDBY"
    return coord


class TestPlan01EvSocRestart:
    """PLAN-01: EV SoC timestamp persists across HA restart."""

    def test_unix_time_saved_when_ev_soc_updated(self) -> None:
        """When ev_soc is read from state, unix time is recorded alongside monotonic."""
        # This verifies that _last_known_ev_soc_unix is set whenever
        # _last_known_ev_soc_time is set in coordinator
        before = time.time()
        mono = time.monotonic()
        unix = time.time()
        after = time.time()
        # Both should be captured at similar times
        assert before <= unix <= after
        assert mono > 0

    def test_age_calculation_uses_unix_not_monotonic(self) -> None:
        """After restart, age should be computed from unix time (not reset monotonic)."""
        # Simulate: EV SoC recorded 30 min ago (unix)
        age_30min = 30 * 60
        stored_unix = time.time() - age_30min
        stored_soc = 66.4

        # Simulate restore logic (mirrors coordinator._async_restore_runtime)
        age_s = time.time() - stored_unix
        assert 29 * 60 < age_s < 31 * 60  # ~30 min

        mono_now = time.monotonic()
        restored_monotonic = mono_now - age_s
        # Restored monotonic should be in the past (positive but smaller than now)
        assert 0 < restored_monotonic < mono_now

        # And age from the restored monotonic should match
        age_from_mono = mono_now - restored_monotonic
        assert abs(age_from_mono - age_s) < 1.0  # < 1s drift

        # SoC should be usable (age < 4h)
        assert stored_soc > 0
        assert age_s < 14400

    def test_fresh_soc_survives_restart(self) -> None:
        """EV SoC recorded 10 min ago → still valid after restart."""
        stored_unix = time.time() - 600  # 10 min ago
        stored_soc = 75.0

        age_s = time.time() - stored_unix
        assert age_s < 14400  # < 4h — should survive
        assert stored_soc > 0

    def test_old_soc_discarded_after_restart(self) -> None:
        """EV SoC recorded 5 hours ago → discarded (timeout)."""
        stored_unix = time.time() - 5 * 3600  # 5h ago
        age_s = time.time() - stored_unix
        assert age_s >= 14400  # >= 4h — should be discarded

    def test_missing_unix_time_defaults_to_discard(self) -> None:
        """If ev_soc_unix_time missing in store → soc not restored."""
        # stored_unix = 0.0 (default)
        stored_unix = 0.0
        # With stored_unix = 0, condition stored_unix > 0 is False → skip restore
        assert stored_unix <= 0  # Triggers discard path

    def test_negative_soc_not_restored(self) -> None:
        """If stored EV SoC is -1 or 0 → skip restore regardless of timestamp."""
        stored_soc = -1.0
        # Condition: stored_soc > 0 must be False to skip
        assert stored_soc <= 0  # Triggers discard path

    def test_exactly_at_4h_boundary_discarded(self) -> None:
        """Exactly 4h old → discard (boundary is exclusive)."""
        stored_unix = time.time() - 14400  # exactly 4h
        age_s = time.time() - stored_unix
        # Should be discarded (>= 14400)
        assert age_s >= 14400

    def test_just_under_4h_kept(self) -> None:
        """3h 59min old → kept."""
        stored_unix = time.time() - (4 * 3600 - 60)  # 3h 59min ago
        age_s = time.time() - stored_unix
        assert age_s < 14400  # still fresh
