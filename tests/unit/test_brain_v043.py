"""Tests for Brain v0.4.3 bat-as-active-buffer (house load compensation).

Test scenarios S1-S7 from brain_v043_bat_active_buffer_spec.md.
"""

from __future__ import annotations

from custom_components.brain.brain import BrainInput
from custom_components.brain.cascade import _compute_bat_grid_target, compute_targets


def _inp(**kwargs) -> BrainInput:
    """Create BrainInput with v0.4.3-relevant defaults."""
    defaults = {
        "grid_w": 0.0,
        "ev_connected": False,
        "ev_soc": 50.0,
        "ev_target_soc": 80.0,
        "force_active": False,
        "force_a": 0.0,
        "bat_can_engage": True,
        "bat_max_discharge_w_now": 5000.0,
        "bat_avg_soc_pct": 80.0,
        "bat_active_buffer_enabled": True,
        "in_night_window": False,
        "in_evening_window": False,
        "house_load_comp_margin_w": 500.0,
        "bat_floor_day_pct": 50.0,
        "bat_floor_evening_pct": 30.0,
        "per_phase_boost_a": 13.0,
        "bat_soc_charge_ceiling_pct": 95.0,
        "bat_max_charge_w_now": 5000.0,
        "per_phase_fuse_warning_a": 14.0,
        "l1_current_a": 0.0,
        "l2_current_a": 0.0,
        "l3_current_a": 0.0,
        "ev_min_charge_w": 1380.0,
        "bat_support_enabled": True,
        "ev_bat_support_min_soc": 20.0,
        "deadband_w": 100.0,
    }
    defaults.update(kwargs)
    return BrainInput(**defaults)


# ── S1: EV disconnected, house 4kW, PV 1kW, bat 80% ─────────────────────────


class TestS1EvDisconnectedHouseLoad:
    def test_target_bat_w_is_grid_excess(self):
        out = compute_targets(_inp(grid_w=3000.0))
        assert out.target_bat_w == -2500.0

    def test_ev_w_is_zero(self):
        out = compute_targets(_inp(grid_w=3000.0))
        assert out.target_ev_w == 0.0

    def test_reason_ev_not_connected(self):
        out = compute_targets(_inp(grid_w=3000.0))
        assert "EV_NOT_CONNECTED" in out.reason
        assert "bat=-2500" in out.reason


# ── S2: BMS cap limits discharge ────────────────────────────────────────────


class TestS2BmsCap:
    def test_capped_at_bat_max(self):
        out = compute_targets(
            _inp(grid_w=5000.0, bat_max_discharge_w_now=4000.0, bat_avg_soc_pct=70.0)
        )
        assert out.target_bat_w == -4000.0

    def test_not_uncapped(self):
        out = compute_targets(
            _inp(grid_w=5000.0, bat_max_discharge_w_now=4000.0, bat_avg_soc_pct=70.0)
        )
        assert out.target_bat_w != -4500.0


# ── S3: Phase-boost at L3=13.8A, no fuse protect ────────────────────────────


class TestS3PhaseBoost:
    def test_boost_applied_below_fuse_limit(self):
        # L3=13.8A > boost_a=13.0 but < fuse_warning_a=14.0 → EV not blocked
        out = compute_targets(
            _inp(
                l3_current_a=13.8,
                grid_w=2000.0,
                ev_connected=False,
            )
        )
        # base = -(2000-500) = -1500, boost: -1500 - 2000 = -3500
        assert out.target_bat_w == -3500.0

    def test_no_boost_when_phase_below_threshold(self):
        out = compute_targets(
            _inp(
                l3_current_a=12.0,
                grid_w=2000.0,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == -1500.0

    def test_boost_respects_bms_cap(self):
        out = compute_targets(
            _inp(
                l3_current_a=14.5,
                grid_w=2000.0,
                ev_connected=False,
                bat_max_discharge_w_now=2500.0,
            )
        )
        # boosted = -1500 - 2000 = -3500, but max_discharge=2500 → -2500
        assert out.target_bat_w == -2500.0


# ── S4: Night window — bat IDLE ──────────────────────────────────────────────


class TestS4NightWindowBatIdle:
    def test_bat_zero_in_night_window(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                in_night_window=True,
                ev_connected=True,
                skip_night_charge=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_ev_min_charge_in_night_window(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                in_night_window=True,
                ev_connected=True,
                skip_night_charge=False,
                ev_min_charge_w=1380.0,
            )
        )
        assert out.target_ev_w == 1380.0
        assert out.target_bat_w == 0.0


# ── S5: bat_active_buffer_enabled=False ──────────────────────────────────────


class TestS5BufferDisabled:
    def test_bat_idle_when_disabled(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_active_buffer_enabled=False,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_reason_still_ev_not_connected(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_active_buffer_enabled=False,
                ev_connected=False,
            )
        )
        assert "EV_NOT_CONNECTED" in out.reason


# ── S6: SoC at floor — no discharge ─────────────────────────────────────────


class TestS6SocAtFloor:
    def test_bat_idle_when_soc_equals_floor(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_avg_soc_pct=50.0,
                bat_floor_day_pct=50.0,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_bat_idle_when_soc_below_floor(self):
        out = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_avg_soc_pct=45.0,
                bat_floor_day_pct=50.0,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_evening_floor_used_in_evening_window(self):
        out_day = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_avg_soc_pct=35.0,
                bat_floor_day_pct=50.0,
                bat_floor_evening_pct=30.0,
                in_evening_window=False,
                ev_connected=False,
            )
        )
        out_eve = compute_targets(
            _inp(
                grid_w=3000.0,
                bat_avg_soc_pct=35.0,
                bat_floor_day_pct=50.0,
                bat_floor_evening_pct=30.0,
                in_evening_window=True,
                ev_connected=False,
            )
        )
        # 35% < day floor (50%) → idle; 35% > evening floor (30%) → discharges
        assert out_day.target_bat_w == 0.0
        assert out_eve.target_bat_w == -2500.0


# ── S7: PER_PHASE_FUSE_PROTECT — bat helps reduce phase current ─────────────


class TestS7FuseProtectBatHelps:
    def test_ev_blocked_bat_active(self):
        out = compute_targets(
            _inp(
                l3_current_a=14.5,
                per_phase_fuse_warning_a=14.0,
                grid_w=2000.0,
                ev_connected=True,
                bat_avg_soc_pct=80.0,
            )
        )
        assert out.target_ev_w == 0.0
        assert out.target_bat_w < 0.0

    def test_reason_contains_fuse_protect(self):
        out = compute_targets(
            _inp(
                l3_current_a=14.5,
                per_phase_fuse_warning_a=14.0,
                grid_w=2000.0,
                ev_connected=True,
            )
        )
        assert "PER_PHASE_FUSE_PROTECT" in out.reason

    def test_bat_w_negative_at_fuse_protect(self):
        out = compute_targets(
            _inp(
                l3_current_a=14.5,
                per_phase_fuse_warning_a=14.0,
                grid_w=2000.0,
                ev_connected=False,
            )
        )
        # base = -1500, boost (14.5 > 13.0): -1500 - 2000 = -3500
        assert out.target_bat_w == -3500.0


# ── _compute_bat_grid_target unit tests ────────────────────────────────────


class TestComputeHouseBatTarget:
    def test_basic_discharge(self):
        inp = _inp(grid_w=2500.0)
        assert _compute_bat_grid_target(inp) == -2000.0

    def test_zero_when_below_margin(self):
        inp = _inp(grid_w=400.0)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_when_at_margin(self):
        inp = _inp(grid_w=500.0)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_when_disabled(self):
        inp = _inp(grid_w=3000.0, bat_active_buffer_enabled=False)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_when_night(self):
        inp = _inp(grid_w=3000.0, in_night_window=True)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_when_bat_cannot_engage(self):
        inp = _inp(grid_w=3000.0, bat_can_engage=False)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_when_soc_at_floor(self):
        inp = _inp(grid_w=3000.0, bat_avg_soc_pct=50.0, bat_floor_day_pct=50.0)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_capped_by_bms(self):
        inp = _inp(grid_w=10000.0, bat_max_discharge_w_now=3000.0)
        assert _compute_bat_grid_target(inp) == -3000.0


# ── S8a: PV surplus absorption ──────────────────────────────────────────────


class TestS8aSurplusAbsorption:
    def test_charges_bat_when_exporting(self):
        # PV 5kW, house 2kW → grid_w = -3000W (exporting 3kW)
        out = compute_targets(
            _inp(
                grid_w=-3000.0,
                bat_avg_soc_pct=60.0,
                bat_soc_charge_ceiling_pct=95.0,
                bat_max_charge_w_now=5000.0,
                ev_connected=False,
            )
        )
        # export_excess = 3000 - 100 = 2900W → target_bat_w = +2900W
        assert out.target_bat_w == 2900.0

    def test_zero_when_bat_full(self):
        out = compute_targets(
            _inp(
                grid_w=-3000.0,
                bat_avg_soc_pct=96.0,
                bat_soc_charge_ceiling_pct=95.0,
                bat_max_charge_w_now=5000.0,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_capped_by_max_charge(self):
        out = compute_targets(
            _inp(
                grid_w=-5000.0,
                bat_avg_soc_pct=50.0,
                bat_soc_charge_ceiling_pct=95.0,
                bat_max_charge_w_now=3000.0,
                ev_connected=False,
            )
        )
        # export_excess = 5000 - 100 = 4900, capped at 3000
        assert out.target_bat_w == 3000.0

    def test_zero_within_deadband(self):
        out = compute_targets(
            _inp(
                grid_w=-50.0,  # exporting 50W — within 100W deadband
                bat_avg_soc_pct=60.0,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_zero_when_disabled(self):
        out = compute_targets(
            _inp(
                grid_w=-3000.0,
                bat_active_buffer_enabled=False,
                ev_connected=False,
            )
        )
        assert out.target_bat_w == 0.0

    def test_zero_when_night(self):
        out = compute_targets(
            _inp(
                grid_w=-3000.0,
                in_night_window=True,
                ev_connected=True,
                skip_night_charge=False,
            )
        )
        assert out.target_bat_w == 0.0


# ── _compute_bat_grid_target charge path unit tests ─────────────────────────


class TestComputeBatGridTargetCharge:
    def test_charge_when_exporting(self):
        inp = _inp(grid_w=-3000.0, bat_avg_soc_pct=60.0)
        assert _compute_bat_grid_target(inp) == 2900.0

    def test_zero_at_ceiling_soc(self):
        inp = _inp(grid_w=-3000.0, bat_avg_soc_pct=95.0, bat_soc_charge_ceiling_pct=95.0)
        assert _compute_bat_grid_target(inp) == 0.0

    def test_zero_within_export_deadband(self):
        inp = _inp(grid_w=-99.0)
        assert _compute_bat_grid_target(inp) == 0.0
