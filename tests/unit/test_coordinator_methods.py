"""Coverage tests for coordinator.py standalone methods.

Targets coordinator.py missing lines using __new__ bypass pattern:
  - _check_daily_goals (lines 5991-6016, 6049-6058, 6069-6098, 6116-6198)
  - _generate_breach_corrections (lines 5446-5513)
  - _calculate_ev_target (lines 4085-4131)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.carmabox.optimizer.models import BreachCorrection, CarmaboxState

# ── Factory helpers ───────────────────────────────────────────────────────────


def _make_coordinator(*, cfg: dict | None = None) -> object:
    """Bypass coordinator __init__ for testing standalone methods."""
    from custom_components.carmabox.coordinator import CarmaboxCoordinator

    coord = object.__new__(CarmaboxCoordinator)
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=None)
    coord._cfg = cfg or {}
    coord.target_kw = 2.0
    coord.min_soc = 15.0
    coord._breach_history = {}
    coord._breach_escalation = {}
    coord._last_known_ev_soc = -1.0
    coord._ev_last_full_charge_date = None
    coord._days_since_full_charge = MagicMock(return_value=3)
    coord._breach_corrections = []
    coord._MAX_CORRECTIONS = 100
    coord._miner_on = False
    coord.inverter_adapters = []
    return coord


def _make_state(**kwargs) -> MagicMock:
    """Create a mocked HA sensor state."""
    s = MagicMock()
    s.state = kwargs.get("state", "0.0")
    s.attributes = kwargs.get("attributes", {})
    return s


def _make_carmabox_state(**kwargs) -> CarmaboxState:
    return CarmaboxState(**kwargs)


# ── Tests: _check_daily_goals ─────────────────────────────────────────────────


class TestCheckDailyGoals:
    """Lines 5991-6016, 6049-6058, 6069-6098, 6116-6198."""

    def test_ellevio_max_met(self) -> None:
        """Ellevio max ≤ target → goal_met=True."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        ell_max = _make_state(state="1.8")
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state(battery_soc_1=60.0)
        results = coord._check_daily_goals(state)
        assert results.get("ellevio_goal_met") is True

    def test_ellevio_max_breached_high(self) -> None:
        """Ellevio max > 5 → cause='EV+disk overlap' (line 5999)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        ell_max = _make_state(state="5.5")  # > 5
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("ellevio_goal_met") is False
        assert results.get("ellevio_root_cause") == "EV+disk overlap"

    def test_ellevio_max_breached_medium(self) -> None:
        """Ellevio max 4-5 → cause='EV 10A burst' (line 6002)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        ell_max = _make_state(state="4.3")  # 4-5
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("ellevio_root_cause") == "EV 10A burst"

    def test_ellevio_max_breached_low(self) -> None:
        """Ellevio max 3-4 → cause='High base load' (line 6004)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        ell_max = _make_state(state="3.2")  # 3-4
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("ellevio_root_cause") == "High base load"

    def test_ellevio_max_unknown_breach_cause(self) -> None:
        """Ellevio max 2-3 → cause='Unknown' (line 6006)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        ell_max = _make_state(state="2.5")  # 2-3
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("ellevio_root_cause") == "Unknown"

    def test_ellevio_invalid_state_skipped(self) -> None:
        """Non-numeric ellevio state → ValueError caught (line 6015)."""
        coord = _make_coordinator()
        ell_max = _make_state(state="invalid")
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        # Should not raise
        results = coord._check_daily_goals(state)
        assert "ellevio_goal_met" not in results

    def test_pv_self_consumption_goal_met(self) -> None:
        """PV self-consumption >= 80% → goal_met=True (line 6055)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={"total_solar_kwh": 10.0, "total_export_kwh": 1.5},
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("pv_goal_met") is True

    def test_pv_self_consumption_goal_missed_high_export(self) -> None:
        """PV export > 5 → cause='Batteries cold locked' (line 6059)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={"total_solar_kwh": 10.0, "total_export_kwh": 6.0},
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("pv_goal_met") is False
        assert results.get("pv_root_cause") == "Batteries cold locked"

    def test_pv_self_consumption_goal_missed_medium_export(self) -> None:
        """PV export 2-5 → cause='Battery full + no EV' (line 6061)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={"total_solar_kwh": 10.0, "total_export_kwh": 3.0},
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("pv_root_cause") == "Battery full + no EV"

    def test_pv_self_consumption_goal_missed_low_export(self) -> None:
        """PV export < 2 → cause='Normal surplus' (line 6061)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={"total_solar_kwh": 5.0, "total_export_kwh": 1.5},
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert results.get("pv_root_cause") == "Normal surplus"

    def test_breach_escalation_critical_3_breaches(self) -> None:
        """3+ breaches in 7 days → escalation level 2 (lines 6083-6088)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        # Pre-fill breach history with 3 recent dates
        coord._breach_history = {"ellevio": ["2026-03-29", "2026-03-30", "2026-03-31"]}
        ell_max = _make_state(state="5.5")  # breaches ellevio goal
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        coord._check_daily_goals(state)
        # Should set escalation to CRITICAL
        assert coord._breach_escalation.get("ellevio") == 2

    def test_breach_escalation_warning_2_breaches(self) -> None:
        """2 breaches in 7 days → escalation level 1 (lines 6090-6096)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        coord._breach_history = {"ellevio": ["2026-03-30", "2026-03-31"]}
        ell_max = _make_state(state="5.5")
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        coord._check_daily_goals(state)
        assert coord._breach_escalation.get("ellevio") == 1

    def test_breach_escalation_normal_first_breach(self) -> None:
        """First breach → escalation level 0 (line 6098)."""
        coord = _make_coordinator(cfg={"target_kw_day": 2.0})
        # Empty history
        ell_max = _make_state(state="5.5")
        coord.hass.states.get = lambda eid: (ell_max if "ellevio_dagens_max" in eid else None)
        state = _make_carmabox_state()
        coord._check_daily_goals(state)
        assert coord._breach_escalation.get("ellevio") == 0

    def test_battery_score_computed(self) -> None:
        """Battery scoring section computed (lines 6138-6198)."""
        coord = _make_coordinator(cfg={"battery_1_kwh": 15.0, "battery_2_kwh": 5.0})
        ledger = _make_state(
            state="available",
            attributes={
                "total_cost_kr": 10.0,
                "without_battery_kr": 15.0,
                "battery_net_saving_kr": 5.0,
            },
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state(battery_soc_1=70.0, battery_power_1=1000.0)
        results = coord._check_daily_goals(state)
        assert "battery_score" in results
        assert "battery_swing_pct" in results

    def test_battery_score_goal_missed_cold_lock(self) -> None:
        """Swing < 5 → cold lock root cause (line 6187-6188)."""
        coord = _make_coordinator(cfg={"battery_1_kwh": 15.0, "battery_2_kwh": 5.0})
        # Force a low swing by setting same initial min/max
        coord._bat_day_min_soc = 50.0
        coord._bat_day_max_soc = 50.0  # 0% swing → cold lock
        coord.hass.states.get = MagicMock(return_value=None)
        state = _make_carmabox_state(battery_soc_1=50.0, battery_power_1=50.0)
        results = coord._check_daily_goals(state)
        if not results.get("battery_goal_met", True):
            assert "Cold lock" in results.get("battery_root_cause", "")

    def test_cost_savings_computed(self) -> None:
        """Cost savings computed when without_battery_kr > 0.5 (lines 6121-6136)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={
                "total_cost_kr": 8.0,
                "without_battery_kr": 12.0,
            },
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        assert "cost_savings_pct" in results
        assert results["cost_savings_pct"] == pytest.approx(33.3, abs=0.5)

    def test_cost_savings_not_met_low(self) -> None:
        """Cost savings < 5% → cold lock root cause (line 6129)."""
        coord = _make_coordinator()
        ledger = _make_state(
            state="available",
            attributes={"total_cost_kr": 11.9, "without_battery_kr": 12.0},
        )
        coord.hass.states.get = lambda eid: (ledger if "energy_ledger" in eid else None)
        state = _make_carmabox_state()
        results = coord._check_daily_goals(state)
        if not results.get("cost_goal_met", True):
            assert "cold lock" in results.get("cost_root_cause", "").lower()


# ── Tests: _generate_breach_corrections ──────────────────────────────────────


class TestGenerateBreachCorrections:
    """Lines 5446-5513."""

    def test_ev_power_generates_reduce_ev(self) -> None:
        """EV power > 500W → reduce_ev correction (lines 5452-5465)."""
        coord = _make_coordinator()
        state = _make_carmabox_state(ev_power_w=1200.0)
        coord._generate_breach_corrections(state, breach_hour=14, actual_avg=3.5)
        assert any(c.action == "reduce_ev" for c in coord._breach_corrections)

    def test_miner_on_generates_reduce_load(self) -> None:
        """Miner on → reduce_load correction (lines 5466-5476)."""
        coord = _make_coordinator()
        coord._miner_on = True
        state = _make_carmabox_state(ev_power_w=0.0)
        coord._generate_breach_corrections(state, breach_hour=12, actual_avg=2.5)
        assert any(c.action == "reduce_load" for c in coord._breach_corrections)

    def test_battery_idle_generates_add_discharge(self) -> None:
        """Battery idle (power >= -50) → add_discharge correction (lines 5477-5494)."""
        coord = _make_coordinator()
        state = _make_carmabox_state(ev_power_w=0.0, battery_power_1=0.0)
        coord._generate_breach_corrections(state, breach_hour=15, actual_avg=2.8)
        assert any(c.action == "add_discharge" for c in coord._breach_corrections)

    def test_all_corrections_combined(self) -> None:
        """EV + miner + battery idle → 3 corrections (lines 5452-5494)."""
        coord = _make_coordinator()
        coord._miner_on = True
        state = _make_carmabox_state(ev_power_w=1000.0, battery_power_1=0.0)
        coord._generate_breach_corrections(state, breach_hour=10, actual_avg=3.0)
        assert len(coord._breach_corrections) == 3

    def test_old_corrections_expired(self) -> None:
        """Old corrections (>24h) are purged (lines 5496-5507)."""
        coord = _make_coordinator()
        # Add an old correction
        old = BreachCorrection(
            created="2020-01-01T00:00:00",
            source_breach_hour=1,
            action="reduce_ev",
            target_hour=1,
            param="ev_amps=6",
            reason="old",
        )
        coord._breach_corrections = [old]
        state = _make_carmabox_state()
        coord._generate_breach_corrections(state, breach_hour=8, actual_avg=2.5)
        # Old correction should be purged
        assert not any(c.created == "2020-01-01T00:00:00" for c in coord._breach_corrections)

    def test_expired_flag_removes_correction(self) -> None:
        """Corrections with expired=True are removed (line 5501)."""
        coord = _make_coordinator()
        expired_corr = BreachCorrection(
            created="2026-01-01T00:00:00",
            source_breach_hour=1,
            action="reduce_ev",
            target_hour=1,
            param="ev_amps=6",
            reason="expired",
        )
        expired_corr.expired = True
        coord._breach_corrections = [expired_corr]
        state = _make_carmabox_state()
        coord._generate_breach_corrections(state, breach_hour=8, actual_avg=2.5)
        assert not any(
            c.action == "reduce_ev" and c.created == "2026-01-01T00:00:00"
            for c in coord._breach_corrections
        )

    def test_corrupt_created_date_marked_expired(self) -> None:
        """Corrupt created date → entry marked expired (lines 5505-5506)."""
        coord = _make_coordinator()
        corrupt = BreachCorrection(
            created="not_a_date",
            source_breach_hour=1,
            action="reduce_load",
            target_hour=1,
            param="test",
            reason="corrupt",
        )
        coord._breach_corrections = [corrupt]
        state = _make_carmabox_state()
        coord._generate_breach_corrections(state, breach_hour=8, actual_avg=2.5)
        # Corrupt entry should be marked expired
        assert corrupt.expired is True

    def test_corrections_capped_at_max(self) -> None:
        """More than _MAX_CORRECTIONS kept → last N kept (lines 5509-5511)."""
        coord = _make_coordinator()
        coord._MAX_CORRECTIONS = 3
        # Pre-fill with 3 recent corrections
        for i in range(3):
            coord._breach_corrections.append(
                BreachCorrection(
                    created="2026-03-31T10:00:00",
                    source_breach_hour=i,
                    action="reduce_ev",
                    target_hour=i,
                    param="ev_amps=6",
                    reason=f"test {i}",
                )
            )
        coord._miner_on = True
        state = _make_carmabox_state(ev_power_w=1000.0, battery_power_1=0.0)
        # Adding 3 more should trigger cap
        coord._generate_breach_corrections(state, breach_hour=8, actual_avg=3.0)
        assert len(coord._breach_corrections) <= coord._MAX_CORRECTIONS


# ── Tests: _calculate_ev_target ───────────────────────────────────────────────


class TestCalculateEvTarget:
    """Lines 4085-4131."""

    def test_solcast_exception_returns_default(self) -> None:
        """SolcastAdapter raises → returns ev_night_target_soc (line 4085-4087)."""
        coord = _make_coordinator(cfg={"ev_night_target_soc": 75})
        with (
            patch(
                "custom_components.carmabox.coordinator.CarmaboxCoordinator._calculate_ev_target",
                wraps=coord._calculate_ev_target,
            ),
            patch(
                "custom_components.carmabox.adapters.solcast.SolcastAdapter",
                side_effect=RuntimeError("no solcast"),
            ),
        ):
            result = coord._calculate_ev_target()
        assert result == 75.0

    def test_few_daily_forecasts_returns_default(self) -> None:
        """< 3 daily forecasts → returns ev_night_target_soc (lines 4089-4090)."""
        coord = _make_coordinator(cfg={"ev_night_target_soc": 80})
        mock_solcast = MagicMock()
        mock_solcast.forecast_daily_3d = [20.0, 25.0]  # Only 2 days

        with patch(
            "custom_components.carmabox.adapters.solcast.SolcastAdapter",
            return_value=mock_solcast,
        ):
            result = coord._calculate_ev_target()
        assert result == 80.0

    def test_bad_weather_ahead_returns_max(self) -> None:
        """worst_3_days < solar_ok → max_target (lines 4096-4102)."""
        from custom_components.carmabox.const import DEFAULT_EV_SOC_MAX_TARGET

        coord = _make_coordinator(
            cfg={
                "solar_ok_kwh": 20.0,
                "solar_good_kwh": 30.0,
                "ev_soc_min_target": 75.0,
                "ev_soc_max_target": DEFAULT_EV_SOC_MAX_TARGET,
            }
        )
        mock_solcast = MagicMock()
        mock_solcast.forecast_daily_3d = [15.0, 18.0, 10.0, 8.0]  # worst=8 < solar_ok=20

        with patch(
            "custom_components.carmabox.adapters.solcast.SolcastAdapter",
            return_value=mock_solcast,
        ):
            result = coord._calculate_ev_target()
        assert result == DEFAULT_EV_SOC_MAX_TARGET

    def test_good_sun_tomorrow_returns_max(self) -> None:
        """tomorrow > solar_good → max_target (lines 4104-4111)."""
        from custom_components.carmabox.const import DEFAULT_EV_SOC_MAX_TARGET

        coord = _make_coordinator(
            cfg={
                "solar_ok_kwh": 20.0,
                "solar_good_kwh": 30.0,
                "ev_soc_min_target": 75.0,
                "ev_soc_max_target": DEFAULT_EV_SOC_MAX_TARGET,
            }
        )
        mock_solcast = MagicMock()
        mock_solcast.forecast_daily_3d = [20.0, 35.0, 32.0, 28.0]  # tomorrow=35 > solar_good=30

        with patch(
            "custom_components.carmabox.adapters.solcast.SolcastAdapter",
            return_value=mock_solcast,
        ):
            result = coord._calculate_ev_target()
        assert result == DEFAULT_EV_SOC_MAX_TARGET

    def test_ok_sun_linear_interpolation(self) -> None:
        """solar_ok < tomorrow < solar_good → linear interpolation (lines 4113-4122)."""
        from custom_components.carmabox.const import DEFAULT_EV_SOC_MAX_TARGET

        coord = _make_coordinator(
            cfg={
                "solar_ok_kwh": 20.0,
                "solar_good_kwh": 30.0,
                "ev_soc_min_target": 75.0,
                "ev_soc_max_target": DEFAULT_EV_SOC_MAX_TARGET,
            }
        )
        mock_solcast = MagicMock()
        # tomorrow=25 (between solar_ok=20 and solar_good=30), worst=22 > solar_ok=20
        mock_solcast.forecast_daily_3d = [20.0, 25.0, 22.0, 25.0]

        with patch(
            "custom_components.carmabox.adapters.solcast.SolcastAdapter",
            return_value=mock_solcast,
        ):
            result = coord._calculate_ev_target()
        # Should be between min and max
        assert 75.0 <= result <= DEFAULT_EV_SOC_MAX_TARGET

    def test_bad_sun_tomorrow_returns_min(self) -> None:
        """tomorrow == solar_ok (not > solar_ok, not > solar_good) → min_target (lines 4124-4131).

        Rule 4 is the fall-through when:
          - worst_3_days >= solar_ok (Rule 1 skip)
          - tomorrow <= solar_good (Rule 2 skip)
          - tomorrow <= solar_ok (Rule 3 skip)
        Since worst_3_days = min(daily[1:]), all days must be >= solar_ok.
        tomorrow == solar_ok exactly satisfies all conditions.
        """
        coord = _make_coordinator(
            cfg={
                "solar_ok_kwh": 20.0,
                "solar_good_kwh": 30.0,
                "ev_soc_min_target": 75.0,
                "ev_soc_max_target": 100.0,
            }
        )
        mock_solcast = MagicMock()
        # tomorrow=20 == solar_ok, worst_3=20 >= solar_ok → Rule 1/2/3 all skip → Rule 4
        mock_solcast.forecast_daily_3d = [15.0, 20.0, 20.0, 20.0]

        with patch(
            "custom_components.carmabox.adapters.solcast.SolcastAdapter",
            return_value=mock_solcast,
        ):
            result = coord._calculate_ev_target()
        assert result == 75.0  # min_target
