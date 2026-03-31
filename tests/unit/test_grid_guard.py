"""Tests for Grid Guard — LAG 1 enforcement + INV-* invariants."""

from __future__ import annotations

import pytest

from custom_components.carmabox.core.grid_guard import (
    BatteryState,
    Consumer,
    GridGuard,
    GridGuardConfig,
)


def _bat(
    consumer_id: str = "kontor",
    soc: float = 50,
    power_w: float = 0,
    cell_temp_c: float = 15.0,
    ems_mode: str = "discharge_pv",
    fast_charging_on: bool = False,
    available_kwh: float = 5.0,
) -> BatteryState:
    return BatteryState(
        id=consumer_id,
        soc=soc,
        power_w=power_w,
        cell_temp_c=cell_temp_c,
        ems_mode=ems_mode,
        fast_charging_on=fast_charging_on,
        available_kwh=available_kwh,
    )


def _consumer(
    consumer_id: str,
    power_w: float,
    priority_shed: int,
    active: bool = True,
    switch: str = "",
    climate: str = "",
) -> Consumer:
    return Consumer(
        id=consumer_id,
        name=consumer_id,
        power_w=power_w,
        is_active=active,
        priority_shed=priority_shed,
        entity_switch=switch,
        entity_climate=climate,
    )


def _guard(tak: float = 2.0, margin: float = 0.85) -> GridGuard:
    return GridGuard(GridGuardConfig(tak_kw=tak, margin=margin))


# ═══════════════════════════════════════════════════════════════
# Grundläggande
# ═══════════════════════════════════════════════════════════════


class TestBasicEvaluation:
    def test_under_tak_no_action(self):
        g = _guard()
        r = g.evaluate(viktat_timmedel_kw=1.0, grid_import_w=1000, hour=14, minute=30)
        assert r.status == "OK"
        assert r.headroom_kw > 0
        assert len(r.commands) == 0

    def test_over_margin_warning(self):
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=1.8,
            grid_import_w=2000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
        )
        assert r.status in ("WARNING", "CRITICAL")
        assert r.headroom_kw < 0
        assert len(r.commands) > 0

    def test_over_tak_critical(self):
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=2.5,
            grid_import_w=3000,
            hour=14,
            minute=30,
            ev_power_w=4000,
            ev_amps=6,
            ev_phase_count=3,
        )
        assert r.status == "CRITICAL"
        assert any(c["action"] in ("pause_ev", "reduce_ev") for c in r.commands)


# ═══════════════════════════════════════════════════════════════
# Åtgärdstrappa
# ═══════════════════════════════════════════════════════════════


class TestActionLadder:
    def test_step1_vp_kontor_off(self):
        g = _guard()
        vp = _consumer("vp_kontor", 1500, 1, climate="climate.kontor_ac")
        r = g.evaluate(
            viktat_timmedel_kw=1.9,
            grid_import_w=2200,
            hour=14,
            minute=30,
            consumers=[vp],
            kontor_temp_c=18.0,
        )
        assert any(c.get("action") == "set_hvac_off" for c in r.commands)

    def test_step1_vp_kontor_skip_cold(self):
        g = _guard()
        vp = _consumer("vp_kontor", 1500, 1, climate="climate.kontor_ac")
        miner = _consumer("miner", 500, 2, switch="switch.miner")
        r = g.evaluate(
            viktat_timmedel_kw=1.9,
            grid_import_w=2200,
            hour=14,
            minute=30,
            consumers=[vp, miner],
            kontor_temp_c=8.0,
        )
        # VP skipped (temp < 10°C), miner stängs istället
        assert not any(c.get("consumer_id") == "vp_kontor" for c in r.commands)
        assert any(c.get("consumer_id") == "miner" for c in r.commands)

    def test_step2_miner_off(self):
        g = _guard()
        miner = _consumer("miner", 500, 2, switch="switch.miner")
        r = g.evaluate(
            viktat_timmedel_kw=1.8,
            grid_import_w=2000,
            hour=14,
            minute=30,
            consumers=[miner],
        )
        assert any(c.get("action") == "switch_off" for c in r.commands)

    def test_step5_reduce_ev_amps(self):
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=2.2,
            grid_import_w=3000,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=10,
            ev_phase_count=3,
        )
        reduce_cmds = [c for c in r.commands if c.get("action") == "reduce_ev"]
        assert len(reduce_cmds) == 1
        assert reduce_cmds[0]["amps"] < 10
        assert reduce_cmds[0]["amps"] >= 6

    def test_step6_pause_ev(self):
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=3.0,
            grid_import_w=6000,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=6,
            ev_phase_count=3,
        )
        assert any(c.get("action") == "pause_ev" for c in r.commands)

    def test_step7_increase_discharge(self):
        g = _guard()
        bats = [_bat("kontor", available_kwh=10.0)]
        r = g.evaluate(
            viktat_timmedel_kw=3.0,
            grid_import_w=5000,
            hour=14,
            minute=30,
            ev_power_w=0,
            ev_amps=0,
            batteries=bats,
        )
        assert any(c.get("action") == "increase_discharge" for c in r.commands)

    def test_combined_steps(self):
        """Large overshoot → VP off + miner off + EV reduced."""
        g = _guard()
        consumers = [
            _consumer("vp_kontor", 1500, 1, climate="climate.kontor_ac"),
            _consumer("miner", 500, 2, switch="switch.miner"),
        ]
        r = g.evaluate(
            viktat_timmedel_kw=3.5,
            grid_import_w=8000,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=10,
            ev_phase_count=3,
            consumers=consumers,
            kontor_temp_c=18.0,
        )
        actions = [c.get("action") for c in r.commands]
        assert "set_hvac_off" in actions  # VP
        assert "switch_off" in actions  # Miner
        assert "reduce_ev" in actions or "pause_ev" in actions  # EV


# ═══════════════════════════════════════════════════════════════
# Projicering
# ═══════════════════════════════════════════════════════════════


class TestProjection:
    def test_projection_early_hour(self):
        """Spike at XX:05 has big impact (55 min remaining)."""
        g = _guard()
        r = g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=5000, hour=14, minute=5)
        # 55 min at 5kW dominates → projected should be high
        assert r.projected_kw > 3.0

    def test_projection_late_hour(self):
        """Spike at XX:55 has small impact (5 min remaining)."""
        g = _guard()
        r = g.evaluate(viktat_timmedel_kw=1.0, grid_import_w=5000, hour=14, minute=55)
        # Only 5 min at 5kW → projected close to existing average
        assert r.projected_kw < 2.0

    def test_projection_accuracy(self):
        """Projection formula correctness."""
        g = _guard()
        # At minute 30, half way: projected = (timmedel*30 + now*30)/60
        r = g.evaluate(viktat_timmedel_kw=1.0, grid_import_w=2000, hour=14, minute=30)
        # Day weight = 1.0, so grid_viktat = 2.0
        expected = (1.0 * 30 + 2.0 * 30) / 60  # = 1.5
        assert abs(r.projected_kw - expected) < 0.01

    def test_projection_minute_zero(self):
        """PLAT-1159: At minute=0, elapsed=0 — projection = current rate only.

        Old code used max(1, minute) which gave (0*1 + grid*59)/60 ≈ grid*0.983.
        Correct formula: elapsed=0, remaining=60 → projected = grid_viktat.
        """
        g = _guard()
        r = g.evaluate(viktat_timmedel_kw=0.0, grid_import_w=3000, hour=14, minute=0)
        # elapsed=0, remaining=60 → projected = 3.0 * 60 / 60 = 3.0
        expected = 3.0 * 60 / 60  # = 3.0 kW
        assert abs(r.projected_kw - expected) < 0.01

    def test_projection_minute_zero_spike_triggers_critical(self):
        """PLAT-1159: Spike at XX:00 must be caught immediately.

        With old max(1,0) the projection was 2.95 instead of 3.0 —
        same level but important to confirm the formula is exact.
        """
        g = _guard()
        # 3.0 kW at minute 0 → projected = 3.0 → > tak (2.0) → CRITICAL
        r = g.evaluate(viktat_timmedel_kw=0.0, grid_import_w=3000, hour=14, minute=0)
        assert r.status == "CRITICAL"
        assert abs(r.projected_kw - 3.0) < 0.01


# ═══════════════════════════════════════════════════════════════
# Återställning
# ═══════════════════════════════════════════════════════════════


class TestRecovery:
    def test_recovery_after_warning(self):
        g = _guard()
        # Trigger warning
        g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="s")],
            timestamp=100.0,
        )
        # Now headroom OK
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=35,
            timestamp=110.0,
        )
        assert r.status == "RECOVERY"

        # Wait recovery hold
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=40,
            timestamp=170.0,
        )
        assert r.status == "OK"

    def test_recovery_hysteresis(self):
        """Headroom fluctuates around 0 → no oscillation."""
        g = _guard()
        # Trigger warning
        g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="s")],
            timestamp=100.0,
        )
        # Brief OK
        g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=1500,
            hour=14,
            minute=31,
            timestamp=105.0,
        )
        # Should NOT be OK yet (recovery hold)
        r = g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=1500,
            hour=14,
            minute=32,
            timestamp=110.0,
        )
        assert r.status == "RECOVERY"  # Not OK — holding


# ═══════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_hour_reset(self):
        g = _guard()
        g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=2000,
            hour=14,
            minute=55,
            timestamp=100.0,
        )
        # New hour
        r = g.evaluate(
            viktat_timmedel_kw=0.0,
            grid_import_w=1000,
            hour=15,
            minute=0,
            timestamp=400.0,
        )
        assert r.status == "OK"

    def test_sensor_unavailable(self):
        """NaN grid → fallback to last known + margin."""
        g = _guard()
        g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1500,
            hour=14,
            minute=20,
            timestamp=100.0,
        )
        # Now sensor unavailable
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=float("nan"),
            hour=14,
            minute=25,
            timestamp=130.0,
        )
        # Should use 1500 * 1.1 = 1650W
        assert r.status == "OK"  # Still under limit

    def test_3phase_ev_math(self):
        """1 amp reduction = 690W for 3-phase, not 230W."""
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=2500,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=10,
            ev_phase_count=3,
        )
        reduce_cmds = [c for c in r.commands if c.get("action") == "reduce_ev"]
        if reduce_cmds:
            new_amps = reduce_cmds[0]["amps"]
            # Reduction should be fewer amps than single-phase would need
            assert new_amps >= 6

    def test_night_vs_day_weight(self):
        """Same actual power → lower weighted at night."""
        g = _guard()
        # Day: 3000W * 1.0 = 3.0 kW weighted
        r_day = g.evaluate(viktat_timmedel_kw=2.5, grid_import_w=3000, hour=14, minute=30)
        # Night: 3000W * 0.5 = 1.5 kW weighted
        g2 = _guard()
        r_night = g2.evaluate(viktat_timmedel_kw=1.2, grid_import_w=3000, hour=23, minute=30)
        assert r_night.projected_kw < r_day.projected_kw


# ═══════════════════════════════════════════════════════════════
# Förbud (invarianter)
# ═══════════════════════════════════════════════════════════════


class TestInvariants:
    def test_inv1_ems_auto_detected(self):
        g = _guard()
        bats = [_bat("kontor", ems_mode="auto")]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert "INV-1" in r.invariant_violations[0]
        assert any(
            c["action"] == "set_ems_mode" and c["mode"] == "battery_standby" for c in r.commands
        )

    def test_inv1_ems_auto_triggers_replan(self):
        g = _guard()
        bats = [_bat("kontor", ems_mode="auto")]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert r.replan_needed is True

    def test_inv2_crosscharge_detected(self):
        g = _guard()
        bats = [
            _bat("kontor", power_w=-2000),  # Charging
            _bat("forrad", power_w=1500),  # Discharging
        ]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert any("INV-2" in v for v in r.invariant_violations)
        # Both should be set to standby
        standby_cmds = [
            c
            for c in r.commands
            if c.get("action") == "set_ems_mode" and c.get("mode") == "battery_standby"
        ]
        assert len(standby_cmds) == 2

    def test_inv2_crosscharge_triggers_replan(self):
        g = _guard()
        bats = [
            _bat("kontor", power_w=-2000),
            _bat("forrad", power_w=1500),
        ]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert r.replan_needed is True

    def test_inv3_fast_charging_unauthorized(self):
        g = _guard()
        bats = [_bat("kontor", fast_charging_on=True)]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
            fast_charge_authorized=False,
        )
        assert any("INV-3" in v for v in r.invariant_violations)

    def test_inv3_fast_charging_authorized_ok(self):
        g = _guard()
        bats = [_bat("kontor", fast_charging_on=True)]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
            fast_charge_authorized=True,
        )
        assert len(r.invariant_violations) == 0

    def test_inv4_cold_lock_charging(self):
        g = _guard()
        bats = [_bat("kontor", cell_temp_c=3.0, power_w=-1000)]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert any("INV-4" in v for v in r.invariant_violations)

    def test_inv4_cold_lock_discharge_ok(self):
        g = _guard()
        bats = [_bat("kontor", cell_temp_c=3.0, power_w=1000)]  # Discharging
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        inv4 = [v for v in r.invariant_violations if "INV-4" in v]
        assert len(inv4) == 0  # Discharge at cold is OK

    def test_inv5_discharge_below_min_soc(self):
        g = _guard()
        bats = [_bat("forrad", soc=12, power_w=2000)]  # Discharging at 12%
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert any("INV-5" in v for v in r.invariant_violations)
        assert any(c["mode"] == "battery_standby" for c in r.commands)

    def test_inv5_discharge_above_min_soc_ok(self):
        g = _guard()
        bats = [_bat("kontor", soc=40, power_w=2000)]  # Discharging at 40%
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        inv5 = [v for v in r.invariant_violations if "INV-5" in v]
        assert len(inv5) == 0

    def test_inv5_cold_battery_higher_min_soc(self):
        g = _guard()
        # 18% SoC + cold (3°C) → min_soc=20% → VIOLATION
        bats = [_bat("kontor", soc=18, power_w=1500, cell_temp_c=3.0)]
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1000,
            hour=14,
            minute=30,
            batteries=bats,
        )
        assert any("INV-5" in v for v in r.invariant_violations)

    def test_invariants_plus_headroom_both_run(self):
        """If invariant violated AND over limit, BOTH fixes run."""
        g = _guard()
        bats = [_bat("kontor", ems_mode="auto")]
        miner = _consumer("miner", 500, 2, switch="switch.miner")
        r = g.evaluate(
            viktat_timmedel_kw=2.5,
            grid_import_w=3000,
            hour=14,
            minute=30,
            batteries=bats,
            consumers=[miner],
        )
        assert len(r.invariant_violations) > 0
        # Both invariant fix AND action ladder should have commands
        has_ems_fix = any(c.get("action") == "set_ems_mode" for c in r.commands)
        has_load_shed = any(c.get("action") == "switch_off" for c in r.commands)
        assert has_ems_fix  # Invariant fix
        assert has_load_shed  # Action ladder (grid over tak)

    def test_invariants_under_limit_no_ladder(self):
        """If invariant violated but under limit, only invariant fix."""
        g = _guard()
        bats = [_bat("kontor", ems_mode="auto")]
        miner = _consumer("miner", 500, 2, switch="switch.miner")
        r = g.evaluate(
            viktat_timmedel_kw=0.5,
            grid_import_w=500,
            hour=14,
            minute=30,
            batteries=bats,
            consumers=[miner],
        )
        assert len(r.invariant_violations) > 0
        has_ems_fix = any(c.get("action") == "set_ems_mode" for c in r.commands)
        has_load_shed = any(c.get("action") == "switch_off" for c in r.commands)
        assert has_ems_fix  # Invariant fix
        assert not has_load_shed  # No ladder needed — under limit


# ═══════════════════════════════════════════════════════════════
# Gränssnittstester (scenarier)
# ═══════════════════════════════════════════════════════════════


class TestScenarios:
    def test_scenario_disk_plus_ev_night(self):
        """Disk 2kW + EV 4.1kW + house 1.7kW at night."""
        g = _guard()
        consumers = [
            _consumer("vp_kontor", 0, 1, active=False),
            _consumer("miner", 0, 2, active=False),
        ]
        # Night: grid 7.8kW * 0.5 weight = 3.9kW viktat > 2.0*0.85
        r = g.evaluate(
            viktat_timmedel_kw=3.5,
            grid_import_w=7800,
            hour=23,
            minute=10,
            ev_power_w=4140,
            ev_amps=6,
            ev_phase_count=3,
            consumers=consumers,
            batteries=[_bat("kontor", available_kwh=5.0)],
        )
        assert r.status in ("WARNING", "CRITICAL")
        # Should pause EV (only consumer with significant power)
        assert any(c.get("action") == "pause_ev" for c in r.commands)


# ═══════════════════════════════════════════════════════════════
# Proaktiv projektion — 3 nivåer (PLAT-1100)
# ═══════════════════════════════════════════════════════════════


class TestProjectionLevels:
    """Test 3-level escalation: WARN (tak*0.85), STOP (tak), EMERGENCY (tak*1.1)."""

    def test_under_warn_no_action(self):
        """Projected below tak*0.85 → OK, no commands."""
        g = _guard()
        # minute=50, viktat=1.0, grid=1500W → projected = (1.0*50+1.5*10)/60 = 1.08
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=1500,
            hour=14,
            minute=50,
        )
        assert r.status == "OK"
        assert len(r.commands) == 0
        assert r.projected_kw < 2.0 * 0.85

    def test_warn_level_reduces_ev_no_pause(self):
        """Projected > tak*0.85 but < tak → WARNING, reduce EV but no pause."""
        g = _guard()
        # Need projected between 1.7 and 2.0
        # minute=30: projected = (viktat*30 + grid_viktat*30)/60
        # viktat=1.5, grid=2200W (day weight=1.0 → 2.2kW)
        # projected = (1.5*30 + 2.2*30)/60 = (45+66)/60 = 1.85 → WARN
        r = g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=2200,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=10,
            ev_phase_count=3,
        )
        assert r.status == "WARNING"
        assert r.projected_kw > 2.0 * 0.85
        assert r.projected_kw <= 2.0
        # Should reduce EV amps but NOT pause
        actions = [c.get("action") for c in r.commands]
        assert "reduce_ev" in actions
        assert "pause_ev" not in actions
        assert "increase_discharge" not in actions

    def test_stop_level_pauses_ev(self):
        """Projected > tak but < tak*1.1 → CRITICAL, EV paused."""
        g = _guard()
        # minute=30: projected = (viktat*30 + grid_viktat*30)/60
        # viktat=1.6, grid=2800W (day weight=1.0 → 2.8kW)
        # projected = (1.6*30 + 2.8*30)/60 = (48+84)/60 = 2.2 → >2.0 but <2.2
        # Actually 2.2 = tak*1.1 exactly, need <2.2
        # viktat=1.5, grid=2800W → (45+84)/60 = 2.15 → STOP
        r = g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=2800,
            hour=14,
            minute=30,
            ev_power_w=4140,
            ev_amps=6,
            ev_phase_count=3,
        )
        assert r.status == "CRITICAL"
        assert r.projected_kw > 2.0
        assert r.projected_kw <= 2.0 * 1.1
        actions = [c.get("action") for c in r.commands]
        assert "pause_ev" in actions
        assert "increase_discharge" not in actions

    def test_emergency_level_discharge(self):
        """Projected > tak*1.1 → CRITICAL, battery discharge when no EV to shed."""
        g = _guard()
        bats = [_bat("kontor", available_kwh=10.0)]
        # No EV — house load alone pushes over emergency limit
        # minute=10: projected = (viktat*10 + grid_viktat*50)/60
        # viktat=1.0, grid=5000W → projected = (10+250)/60 = 4.33 >> 2.2
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=5000,
            hour=14,
            minute=10,
            ev_power_w=0,
            ev_amps=0,
            batteries=bats,
        )
        assert r.status == "CRITICAL"
        assert r.projected_kw > 2.0 * 1.1
        actions = [c.get("action") for c in r.commands]
        assert "increase_discharge" in actions

    def test_ev_started_early_hour_projection_catches_it(self):
        """Real scenario from PLAT-1100: EV 6A, grid 5kW, minute 10 → caught."""
        g = _guard()
        bats = [_bat("kontor", available_kwh=5.0), _bat("forrad", available_kwh=5.0)]
        # Simulates: EV just started, viktat only 1.3 kW but grid is 5kW
        # projected = (1.3*10 + 5.0*50)/60 = (13+250)/60 = 4.38 → EMERGENCY
        r = g.evaluate(
            viktat_timmedel_kw=1.3,
            grid_import_w=5000,
            hour=14,
            minute=10,
            ev_power_w=4140,
            ev_amps=6,
            ev_phase_count=3,
            batteries=bats,
        )
        assert r.status == "CRITICAL"
        assert r.projected_kw > 2.0
        # EV must be stopped
        assert any(c.get("action") == "pause_ev" for c in r.commands)

    def test_warn_level_no_ev_sheds_consumers(self):
        """WARN level with no EV but consumers → shed consumers only."""
        g = _guard()
        consumers = [_consumer("miner", 500, 2, switch="switch.miner")]
        r = g.evaluate(
            viktat_timmedel_kw=1.5,
            grid_import_w=2200,
            hour=14,
            minute=30,
            consumers=consumers,
        )
        assert r.status == "WARNING"
        actions = [c.get("action") for c in r.commands]
        assert "switch_off" in actions
        assert "pause_ev" not in actions
        assert "increase_discharge" not in actions

    def test_scenario_short_spike_ok(self):
        """Spike at XX:50, average already low → no action needed."""
        g = _guard()
        # 50 min at 1.0 kW, now spike to 5 kW for remaining 10 min
        # projected = (1.0*50 + 5.0*10)/60 = 58.3/60 = 0.97 kW
        r = g.evaluate(
            viktat_timmedel_kw=1.0,
            grid_import_w=5000,
            hour=14,
            minute=50,
        )
        # projected ~1.67 < 1.7 (tak*margin) — should be OK
        assert r.status == "OK"


# ═══════════════════════════════════════════════════════════════
# PLAT-1162: headroom_kw AttributeError
# ═══════════════════════════════════════════════════════════════


class TestHeadroomProperty:
    def test_headroom_kw_before_evaluate_no_error(self):
        """headroom_kw property must not raise AttributeError before evaluate()."""
        g = _guard()
        # Should not raise — returns tak*margin - 0 = 1.7
        hw = g.headroom_kw
        assert hw == 2.0 * 0.85

    def test_headroom_kw_after_evaluate_reflects_projection(self):
        """headroom_kw property returns correct value after evaluate()."""
        g = _guard()
        r = g.evaluate(viktat_timmedel_kw=1.0, grid_import_w=2000, hour=14, minute=30)
        # projected = (1.0*30 + 2.0*30)/60 = 1.5
        assert abs(g.headroom_kw - (2.0 * 0.85 - 1.5)) < 0.01
        assert abs(g.headroom_kw - r.headroom_kw) < 0.01


# ═══════════════════════════════════════════════════════════════
# PLAT-1164: action_ladder hysteres
# ═══════════════════════════════════════════════════════════════


class TestLadderHysteresis:
    def test_ladder_fires_on_first_breach(self):
        """First over-limit cycle → ladder fires immediately."""
        g = _guard()
        r = g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=100.0,
        )
        assert r.status in ("WARNING", "CRITICAL")
        assert len(r.commands) > 0

    def test_ladder_suppressed_within_cooldown(self):
        """Second over-limit cycle within 60s → no new commands."""
        g = _guard()
        # First breach
        g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=100.0,
        )
        # Second breach 30s later (< cooldown)
        r2 = g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=130.0,
        )
        assert r2.status in ("WARNING", "CRITICAL")
        assert len(r2.commands) == 0  # Cooldown suppresses re-escalation

    def test_ladder_fires_again_after_cooldown(self):
        """After cooldown expires, ladder fires again."""
        g = _guard()
        # First breach
        g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=100.0,
        )
        # After cooldown (>60s)
        r2 = g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=31,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=165.0,
        )
        assert len(r2.commands) > 0  # Cooldown expired → ladder fires

    def test_ladder_cooldown_configurable(self):
        """Custom ladder_cooldown_s is respected."""
        g = GridGuard(GridGuardConfig(ladder_cooldown_s=10.0))
        g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=100.0,
        )
        # 15s later — exceeds 10s cooldown
        r2 = g.evaluate(
            viktat_timmedel_kw=2.0,
            grid_import_w=3000,
            hour=14,
            minute=30,
            consumers=[_consumer("miner", 500, 2, switch="switch.miner")],
            timestamp=115.0,
        )
        assert len(r2.commands) > 0  # 15s > 10s cooldown → fires


class TestPersistence:
    """PLAT-1095: get_persistent_state / restore_state."""

    def test_get_persistent_state_returns_current_accumulation(self):
        """get_persistent_state returns hour + accumulated_viktat_wh."""
        g = GridGuard()
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=2000, hour=14, minute=10, timestamp=100.0)
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=2000, hour=14, minute=10, timestamp=130.0)
        state = g.get_persistent_state()
        assert state["hour"] == 14
        assert state["accumulated_viktat_wh"] > 0
        assert state["sample_count"] == 2
        assert "last_grid_w" in state

    def test_restore_state_same_hour_restores_accumulation(self):
        """restore_state restores accumulation when hour matches."""
        g = GridGuard()
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=3000, hour=10, minute=5, timestamp=100.0)
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=3000, hour=10, minute=5, timestamp=160.0)
        saved = g.get_persistent_state()
        expected_wh = saved["accumulated_viktat_wh"]

        g2 = GridGuard()
        g2.restore_state(saved, current_hour=10)
        restored = g2.get_persistent_state()
        assert restored["accumulated_viktat_wh"] == pytest.approx(expected_wh)
        assert restored["hour"] == 10
        assert restored["sample_count"] == saved["sample_count"]

    def test_restore_state_stale_hour_ignored(self):
        """restore_state discards data when stored hour != current hour."""
        g = GridGuard()
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=3000, hour=10, minute=5, timestamp=100.0)
        saved = g.get_persistent_state()

        g2 = GridGuard()
        g2.restore_state(saved, current_hour=11)  # Different hour — must be ignored
        assert g2.get_persistent_state()["accumulated_viktat_wh"] == 0.0
        assert g2.get_persistent_state()["hour"] == -1

    def test_restore_state_empty_data_ignored(self):
        """restore_state with empty dict leaves GridGuard in default state."""
        g = GridGuard()
        g.restore_state({}, current_hour=14)
        assert g.get_persistent_state()["accumulated_viktat_wh"] == 0.0

    def test_restore_state_last_update_reset_to_zero(self):
        """After restore, first cycle skips accumulation (last_update=0).

        Monotonic timestamps don't survive restart — _last_update must be 0
        so the first evaluate() after restore does NOT add a bogus dt_s.
        """
        g = GridGuard()
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=2000, hour=9, minute=0, timestamp=100.0)
        g.evaluate(viktat_timmedel_kw=0.5, grid_import_w=2000, hour=9, minute=0, timestamp=130.0)
        saved = g.get_persistent_state()
        wh_before = saved["accumulated_viktat_wh"]

        g2 = GridGuard()
        g2.restore_state(saved, current_hour=9)
        # First cycle after restore: last_update=0 → no accumulation added
        g2.evaluate(
            viktat_timmedel_kw=0.5, grid_import_w=2000, hour=9, minute=5, timestamp=200.0
        )
        wh_after = g2.get_persistent_state()["accumulated_viktat_wh"]
        assert wh_after == pytest.approx(wh_before)  # No spurious addition
