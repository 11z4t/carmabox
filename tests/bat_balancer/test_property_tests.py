"""Property tests PT-BAL-1..5,7 (spec BAL-10-BAT §PROPERTY_TESTS, SG-13).

PT-BAL-1: SoC-equalization is zero-sum (INV-24) — Hypothesis randomized
PT-BAL-2: Bias ≤ MAX_BIAS_W per bank (INV-24)
PT-BAL-3: Under threshold → no bias (INV-24)
PT-BAL-4: Brain opacity (actual_total_w == brain_target)
PT-BAL-5: N=1 → no equalization, full target to one bank
PT-BAL-7: No sign-flip without ramp via 0 (INV-34)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.distribution_engine import distribute_target_to_banks
from custom_components.bat_balancer.models import BankConfig, BankState, SensorSnapshot
from custom_components.bat_balancer.sign_state_machine import SignStateMachine

try:
    from hypothesis import assume, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

HYPOTHESIS_AVAILABLE = pytest.mark.skipif(not HAS_HYPOTHESIS, reason="hypothesis not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _two_bank_setup(
    kontor_soc: float = 40.0,
    forrad_soc: float = 15.0,
    target_w: float = 4000.0,
    eq_threshold: float = 5.0,
    eq_max_bias: float = 500.0,
    kontor_cap: float = 14.0,
    forrad_cap: float = 6.0,
    kontor_max_charge: float = 7589.0,
    forrad_max_charge: float = 2473.0,
    kontor_max_discharge: float = 7589.0,
    forrad_max_discharge: float = 2473.0,
):
    configs = {
        "kontor": BankConfig(
            id="kontor",
            capacity_kwh=kontor_cap,
            max_charge_w=kontor_max_charge,
            max_discharge_w=kontor_max_discharge,
        ),
        "forrad": BankConfig(
            id="forrad",
            capacity_kwh=forrad_cap,
            max_charge_w=forrad_max_charge,
            max_discharge_w=forrad_max_discharge,
        ),
    }
    states = {
        "kontor": BankState(bank_id="kontor", current_soc=kontor_soc, is_online=True),
        "forrad": BankState(bank_id="forrad", current_soc=forrad_soc, is_online=True),
    }
    snap = SensorSnapshot(
        brain_target_bat_w=target_w,
        soc_equalization_threshold_pct=eq_threshold,
        soc_equalization_max_bias_w=eq_max_bias,
    )
    return configs, states, snap


# ---------------------------------------------------------------------------
# PT-BAL-1: Zero-sum (table-driven + optional Hypothesis)
# ---------------------------------------------------------------------------


class TestPTBAL1ZeroSum:
    """SoC-equalization preserves total power (zero-sum)."""

    @pytest.mark.parametrize(
        "k_soc,f_soc,target_w",
        [
            (40.0, 15.0, 4000.0),  # charge, 25% divergence
            (40.0, 15.0, -4000.0),  # discharge
            (80.0, 20.0, 3000.0),  # larger divergence
            (80.0, 20.0, -3000.0),
            (60.0, 55.0, 2000.0),  # small divergence (still > 5%)
            (90.0, 10.0, 5000.0),  # extreme divergence
        ],
    )
    def test_zero_sum_parametrized(self, k_soc, f_soc, target_w):
        configs, states, snap = _two_bank_setup(
            kontor_soc=k_soc,
            forrad_soc=f_soc,
            target_w=target_w,
        )
        result = distribute_target_to_banks(target_w, configs, states, snap)

        total = sum(result.targets.values())
        if result.bms_cap_suppressed_w > 0.0:
            # D5: overflow suppressed to protect SoC balance — underdelivery is correct
            assert (
                abs(total) <= abs(target_w) + 1.0
            ), f"D5 over-delivered: |sum|={abs(total):.2f} > target={abs(target_w):.2f}"
        else:
            assert total == pytest.approx(
                target_w, abs=1.0
            ), f"Zero-sum violated: targets sum={total:.2f} target={target_w}"

    @HYPOTHESIS_AVAILABLE
    @given(
        k_soc=st.floats(min_value=10.0, max_value=89.0),
        f_soc=st.floats(min_value=10.0, max_value=89.0),
        target_w=st.floats(min_value=-8000.0, max_value=8000.0),
    )
    @settings(max_examples=200)
    def test_zero_sum_hypothesis(self, k_soc, f_soc, target_w):
        assume(abs(k_soc - f_soc) > 5.0)  # ensure equalization is active
        assume(abs(target_w) > 10.0)  # avoid near-zero targets
        # Equal capacity to decouple headroom-weighting from zero-sum check
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=8000.0, max_discharge_w=8000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=8000.0, max_discharge_w=8000.0
            ),
        }
        states = {
            "kontor": BankState(bank_id="kontor", current_soc=k_soc, is_online=True),
            "forrad": BankState(bank_id="forrad", current_soc=f_soc, is_online=True),
        }
        snap = SensorSnapshot(
            brain_target_bat_w=target_w,
            soc_equalization_threshold_pct=5.0,
            soc_equalization_max_bias_w=500.0,
        )
        result = distribute_target_to_banks(target_w, configs, states, snap)

        total = sum(result.targets.values())
        assert (
            abs(total - target_w) < 1.0
        ), f"Hypothesis: zero-sum violated total={total:.2f} target={target_w:.2f}"


# ---------------------------------------------------------------------------
# PT-BAL-2: Bias ≤ MAX_BIAS_W per bank
# ---------------------------------------------------------------------------


class TestPTBAL2MaxBias:
    def test_extreme_divergence_bias_capped(self):
        """Extreme (0% vs 100%) → raw bias per bank ≤ MAX_BIAS_W (spec PT-BAL-2).

        With one bank at 100% SoC in charge mode, its headroom-weight is 0,
        so it gets 0W base allocation. The equalization bias is still raw-capped
        at MAX_BIAS_W (500W) and the result is re-normalized to preserve zero-sum.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        states = {
            "kontor": BankState(bank_id="kontor", current_soc=0.0, is_online=True),
            "forrad": BankState(bank_id="forrad", current_soc=100.0, is_online=True),
        }
        snap = SensorSnapshot(
            brain_target_bat_w=4000.0,
            soc_equalization_threshold_pct=5.0,
            soc_equalization_max_bias_w=500.0,
            soc_equalization_full_bias_threshold_pct=101.0,  # disable full-bias; test targets progressive path
        )
        result = distribute_target_to_banks(4000.0, configs, states, snap)

        # Raw bias was clipped to MAX_BIAS_W (500W) per bank (PT-BAL-2 spec assertion)
        assert result.equalization_bias_max_w <= 500.0 + 1.0
        # Zero-sum preserved
        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)

    def test_normal_divergence_bias_within_cap(self):
        """Progressive-bias scenario: 8pp divergence (> 3pp threshold, < 15pp full-bias) → deviation ≤ 500W.

        Uses high BMS caps to avoid capping and D5 interaction.
        25pp divergence would trigger full-bias (not progressive) and is tested separately.
        """
        configs, states, snap = _two_bank_setup(
            kontor_soc=45.0,
            forrad_soc=37.0,
            target_w=-4000.0,
            kontor_cap=7.0,
            forrad_cap=7.0,
            kontor_max_charge=8000.0,
            forrad_max_charge=8000.0,
        )
        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        mean = (result.targets["kontor"] + result.targets["forrad"]) / 2.0
        for k in ["kontor", "forrad"]:
            bias = abs(result.targets[k] - mean)
            assert bias <= 500.0 + 1.0, f"Bias for {k} exceeds MAX_BIAS_W: {bias:.1f}W"


# ---------------------------------------------------------------------------
# PT-BAL-3: Under threshold → no bias
# ---------------------------------------------------------------------------


class TestPTBAL3UnderThreshold:
    def test_at_threshold_no_equalization(self):
        """Divergence exactly = threshold (5%) → no equalization (strictly greater)."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=50.0,
            forrad_soc=45.0,  # divergence = 5% exactly
            target_w=4000.0,
            eq_threshold=5.0,
        )
        result = distribute_target_to_banks(4000.0, configs, states, snap)

        assert result.equalization_active is False

    def test_just_below_threshold_no_equalization(self):
        """Divergence 4% < 5% threshold → no bias."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=49.0,
            forrad_soc=45.0,  # divergence = 4%
            target_w=4000.0,
            eq_threshold=5.0,
        )
        result = distribute_target_to_banks(4000.0, configs, states, snap)

        assert result.equalization_active is False

    def test_zero_divergence_no_equalization(self):
        """Both banks same SoC → no equalization."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=50.0,
            forrad_soc=50.0,
            target_w=4000.0,
            eq_threshold=5.0,
        )
        result = distribute_target_to_banks(4000.0, configs, states, snap)

        assert result.equalization_active is False


# ---------------------------------------------------------------------------
# PT-BAL-4: Brain opacity (actual_total_w == brain_target_bat_w)
# ---------------------------------------------------------------------------


class TestPTBAL4BrainOpacity:
    def test_actual_total_matches_brain_target(self):
        """Brain sees actual_total_w ≈ abs(brain_target_bat_w) (pre-BMS-cap)."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=40.0,
            forrad_soc=15.0,
            target_w=3000.0,
        )
        result = distribute_target_to_banks(3000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(3000.0, abs=1.0)
        # brain_target_bat_w is unchanged by equalization
        assert snap.brain_target_bat_w == pytest.approx(3000.0)

    def test_equalization_does_not_change_brain_input(self):
        """Equalization must not modify brain_target_bat_w."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=80.0,
            forrad_soc=10.0,
            target_w=2000.0,
        )
        original_brain_target = snap.brain_target_bat_w

        distribute_target_to_banks(2000.0, configs, states, snap)

        assert snap.brain_target_bat_w == pytest.approx(original_brain_target)

    def test_discharge_actual_total_matches(self):
        """Discharge (positive target): actual_total_w == brain_target_bat_w pre-BMS-cap."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=80.0,
            forrad_soc=40.0,
            target_w=3000.0,
        )
        result = distribute_target_to_banks(3000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(3000.0, abs=1.0)


# ---------------------------------------------------------------------------
# PT-BAL-5: N=1 → no equalization
# ---------------------------------------------------------------------------


class TestPTBAL5SingleBank:
    def test_n1_full_target_to_single_bank(self):
        """N=1 online → full target allocated, equalization never active."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=60.0,
            forrad_soc=30.0,
            target_w=2000.0,
        )
        states["forrad"].is_online = False

        result = distribute_target_to_banks(2000.0, configs, states, snap)

        assert result.equalization_active is False
        assert result.targets["kontor"] == pytest.approx(2000.0, abs=2.0)
        assert result.targets["forrad"] == pytest.approx(0.0)

    def test_n1_discharge(self):
        """N=1 discharge: full target to kontor, forrad=0."""
        configs, states, snap = _two_bank_setup(
            kontor_soc=70.0,
            forrad_soc=30.0,
            target_w=-1500.0,
        )
        states["forrad"].is_online = False

        result = distribute_target_to_banks(-1500.0, configs, states, snap)

        assert result.equalization_active is False
        assert result.targets["forrad"] == pytest.approx(0.0)
        assert abs(result.targets["kontor"]) == pytest.approx(1500.0, abs=2.0)


# ---------------------------------------------------------------------------
# PT-BAL-7: No sign-flip without ramp via 0 (INV-34)
# ---------------------------------------------------------------------------


class TestPTBAL7SignFlipViaZero:
    def test_direct_flip_inserts_zero_tick(self):
        """PT-BAL-7: last=-2000 (charge) → +2000 (discharge) → zero-tick inserted."""
        sm = SignStateMachine()

        # First tick: establish CHARGING state (negative = charge)
        out1 = sm.tick(-2000.0)
        assert out1 == pytest.approx(-2000.0)
        assert sm.state.value == "charging"

        # Second tick: direct flip to discharge (positive = discharge)
        out2 = sm.tick(2000.0)
        assert out2 == pytest.approx(0.0)  # zero-tick (INV-34)
        assert sm.sign_flip_pending is True

        # Third tick: now discharging
        out3 = sm.tick(2000.0)
        assert out3 == pytest.approx(2000.0)
        assert sm.sign_flip_pending is False

    def test_no_ramp_on_same_direction(self):
        """Charge→charge: no zero-tick inserted."""
        sm = SignStateMachine()
        sm.tick(1000.0)  # establish CHARGING

        out = sm.tick(2000.0)
        assert out == pytest.approx(2000.0)
        assert sm.sign_flip_pending is False

    def test_idle_to_charge_no_ramp(self):
        """IDLE→charge: no zero-tick (no direction flip)."""
        sm = SignStateMachine()
        out = sm.tick(1500.0)
        assert out == pytest.approx(1500.0)
        assert sm.sign_flip_pending is False

    def test_charge_to_idle_no_ramp(self):
        """Charge→idle (0): no zero-tick needed."""
        sm = SignStateMachine()
        sm.tick(1000.0)

        out = sm.tick(0.0)
        assert out == pytest.approx(0.0)
        assert sm.sign_flip_pending is False

    def test_discharge_to_charge_zero_tick(self):
        """Discharge→charge: zero-tick inserted."""
        sm = SignStateMachine()
        sm.tick(-1500.0)  # establish DISCHARGING

        out = sm.tick(1500.0)
        assert out == pytest.approx(0.0)
        assert sm.sign_flip_pending is True

        out2 = sm.tick(1500.0)
        assert out2 == pytest.approx(1500.0)
        assert sm.sign_flip_pending is False

    def test_multiple_flips_each_gets_zero_tick(self):
        """Multiple flips: each gets its own zero-tick."""
        sm = SignStateMachine()

        sm.tick(1000.0)  # CHARGING
        assert sm.tick(-1000.0) == pytest.approx(0.0)  # zero-tick
        assert sm.tick(-1000.0) == pytest.approx(-1000.0)  # DISCHARGING
        assert sm.tick(1000.0) == pytest.approx(0.0)  # zero-tick again
        assert sm.tick(1000.0) == pytest.approx(1000.0)  # CHARGING again

    def test_zero_tick_uses_next_call_destination(self):
        """After zero-tick, the next call to tick() returns the desired target."""
        sm = SignStateMachine()
        sm.tick(500.0)

        sm.tick(-3000.0)  # zero-tick inserted, -3000 stored
        out = sm.tick(-3000.0)  # should return -3000 (ramp complete)
        assert out == pytest.approx(-3000.0)
