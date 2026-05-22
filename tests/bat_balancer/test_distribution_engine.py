"""Unit tests for distribution_engine (spec BAL-10-BAT §7+§7a+§11).

Covers: T1.1–T1.10 + iter-3 edge-cases (N=0, overflow-both-capped, partial-overflow, sign-flip).
Zero HA-deps — pure Python.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT,
    SOC_EQ_THRESHOLD_DEFAULT_PCT,
    RejectedReason,
)
from custom_components.bat_balancer.distribution_engine import distribute_target_to_banks
from custom_components.bat_balancer.models import (
    BankConfig,
    BankState,
    BatBalancerStatus,
    SensorSnapshot,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_configs(
    kontor_cap: float = 14.0,
    forrad_cap: float = 6.0,
    kontor_max_charge: float = 7589.0,
    forrad_max_charge: float = 2473.0,
    kontor_max_discharge: float = 7589.0,
    forrad_max_discharge: float = 2473.0,
) -> dict:
    return {
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


def _make_states(
    kontor_soc: float = 50.0,
    forrad_soc: float = 50.0,
    kontor_online: bool = True,
    forrad_online: bool = True,
    kontor_bms_charge: float | None = None,
    forrad_bms_charge: float | None = None,
    kontor_bms_discharge: float | None = None,
    forrad_bms_discharge: float | None = None,
) -> dict:
    return {
        "kontor": BankState(
            bank_id="kontor",
            current_soc=kontor_soc,
            is_online=kontor_online,
            bms_max_charge_w=kontor_bms_charge,
            bms_max_discharge_w=kontor_bms_discharge,
        ),
        "forrad": BankState(
            bank_id="forrad",
            current_soc=forrad_soc,
            is_online=forrad_online,
            bms_max_charge_w=forrad_bms_charge,
            bms_max_discharge_w=forrad_bms_discharge,
        ),
    }


def _make_snap(
    brain_w: float = 0.0,
    eq_threshold: float = SOC_EQ_THRESHOLD_DEFAULT_PCT,
    eq_max_bias: float = 500.0,
    eq_full_bias_threshold: float = SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT,
) -> SensorSnapshot:
    return SensorSnapshot(
        brain_target_bat_w=brain_w,
        soc_equalization_threshold_pct=eq_threshold,
        soc_equalization_max_bias_w=eq_max_bias,
        soc_equalization_full_bias_threshold_pct=eq_full_bias_threshold,
    )


# ---------------------------------------------------------------------------
# T1.x — Basic distribution
# ---------------------------------------------------------------------------


class TestBasicDistribution:
    """T1.1–T1.5 — headroom-weighted distribution without SoC equalization."""

    def test_t1_1_equal_banks_equal_soc_charge(self):
        """T1.1: target=-2000W (charge), 2 equal banks at 50% SoC → -1000W each."""
        configs = _make_configs(kontor_cap=7.0, forrad_cap=7.0)
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=-2000.0, eq_threshold=100.0)  # disable equalization

        result = distribute_target_to_banks(-2000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(2000.0, abs=0.5)
        assert result.targets["kontor"] == pytest.approx(result.targets["forrad"], abs=0.5)
        assert result.status == BatBalancerStatus.OK
        assert result.rejected_w == pytest.approx(0.0, abs=0.1)

    def test_t1_2_discharge_higher_soc_gets_more(self):
        """T1.2: target=+2000W (discharge), kontor=80% forrad=30% → kontor discharges more."""
        configs = _make_configs()
        states = _make_states(kontor_soc=80.0, forrad_soc=30.0)
        snap = _make_snap(brain_w=2000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(2000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(2000.0, abs=1.0)
        # Kontor has higher SoC → gets more discharge (more positive)
        assert abs(result.targets["kontor"]) > abs(result.targets["forrad"])

    def test_t1_3_charge_both_capped(self):
        """T1.3: target=-5000W (charge), both banks max 2000W → both capped, actual=4000W."""
        configs = _make_configs(
            kontor_max_charge=2000.0,
            forrad_max_charge=2000.0,
            kontor_cap=7.0,
            forrad_cap=7.0,
        )
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=-5000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-5000.0, configs, states, snap)

        assert result.targets["kontor"] == pytest.approx(-2000.0, abs=1.0)
        assert result.targets["forrad"] == pytest.approx(-2000.0, abs=1.0)
        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)
        assert result.rejected_w == pytest.approx(1000.0, abs=1.0)

    def test_t1_4_discharge_full_offer_delivered(self):
        """T1.4 (pure-slave): discharge +3000W → exactly 3000W distributed (no ZG-12 cap).

        bat_balancer is a pure slave — anti-export is Brain's responsibility.
        """
        configs = _make_configs(kontor_cap=7.0, forrad_cap=7.0)
        states = _make_states(kontor_soc=60.0, forrad_soc=60.0)
        snap = _make_snap(brain_w=3000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(3000.0, configs, states, snap)

        total = sum(abs(result.targets[k]) for k in ["kontor", "forrad"])
        assert total == pytest.approx(3000.0, abs=1.0)

    def test_t1_5_asymmetric_capacity(self):
        """T1.5: 2 banks 14kWh/6kWh, target=-4000W (charge) → weight-based distribution."""
        configs = _make_configs(kontor_cap=14.0, forrad_cap=6.0)
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=-4000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)
        # Kontor (14kWh) gets proportionally more than forrad (6kWh)
        # Weights: kontor=14*(100-50)=700, forrad=6*(100-50)=300, total=1000
        # Expected: kontor=-2800W, forrad=-1200W
        assert result.targets["kontor"] == pytest.approx(-2800.0, abs=5.0)
        assert result.targets["forrad"] == pytest.approx(-1200.0, abs=5.0)


# ---------------------------------------------------------------------------
# Iter-3 edge-cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_n0_no_banks_online(self):
        """Iter-3: N=0 — all banks offline → rejected_w=|target|."""
        configs = _make_configs()
        states = _make_states(kontor_online=False, forrad_online=False)
        snap = _make_snap(brain_w=5000.0)

        result = distribute_target_to_banks(5000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(0.0)
        assert result.rejected_w == pytest.approx(5000.0)
        assert result.rejected_reason == RejectedReason.NO_AVAILABLE_BANKS
        assert result.status == BatBalancerStatus.ERROR
        assert result.targets["kontor"] == pytest.approx(0.0)
        assert result.targets["forrad"] == pytest.approx(0.0)

    def test_overflow_both_banks_capped(self):
        """Iter-3: both banks capped below per-bank → rejected_w > 0."""
        configs = _make_configs(
            kontor_max_charge=1500.0,
            forrad_max_charge=2000.0,
            kontor_cap=7.0,
            forrad_cap=7.0,
        )
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=-6000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-6000.0, configs, states, snap)

        assert result.targets["kontor"] == pytest.approx(-1500.0, abs=1.0)
        assert result.targets["forrad"] == pytest.approx(-2000.0, abs=1.0)
        assert result.actual_total_w == pytest.approx(3500.0, abs=1.0)
        assert result.rejected_w == pytest.approx(2500.0, abs=1.0)
        assert result.rejected_reason == RejectedReason.BMS_CAP_AGGREGATE

    def test_partial_overflow_fully_redistributed(self):
        """Iter-3: kontor capped, forrad absorbs overflow → rejected_w=0."""
        configs = _make_configs(
            kontor_max_charge=1500.0,
            forrad_max_charge=5000.0,
            kontor_cap=7.0,
            forrad_cap=7.0,
        )
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=-4000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        assert result.actual_total_w == pytest.approx(4000.0, abs=2.0)
        assert result.rejected_w == pytest.approx(0.0, abs=1.0)
        assert result.targets["kontor"] == pytest.approx(-1500.0, abs=1.0)
        assert result.targets["forrad"] == pytest.approx(-2500.0, abs=1.0)

    def test_target_zero_standby(self):
        """target=0 → STANDBY all banks."""
        configs = _make_configs()
        states = _make_states()
        snap = _make_snap(brain_w=0.0)

        result = distribute_target_to_banks(0.0, configs, states, snap)

        assert result.targets["kontor"] == pytest.approx(0.0)
        assert result.targets["forrad"] == pytest.approx(0.0)
        assert result.actual_total_w == pytest.approx(0.0)
        assert result.status == BatBalancerStatus.OK

    def test_one_bank_offline_redistribution(self):
        """T2.1: 1 bank offline → full target to remaining bank."""
        configs = _make_configs()
        states = _make_states(forrad_online=False)
        snap = _make_snap(brain_w=-2000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-2000.0, configs, states, snap)

        assert result.targets["forrad"] == pytest.approx(0.0)
        assert result.actual_total_w == pytest.approx(2000.0, abs=2.0)
        assert result.targets["kontor"] == pytest.approx(-2000.0, abs=2.0)


# ---------------------------------------------------------------------------
# T1.6–T1.10 — SoC equalization (INV-24)
# ---------------------------------------------------------------------------


class TestSoCEqualization:
    """Tests for INV-24 SoC-equalization (spec §7a)."""

    def test_t1_6_charge_lower_soc_gets_more(self):
        """T1.6: K=40%, F=15% (div=25%>5%), target=-4000W (charge), EQUAL capacity.

        Spec §7a example uses equal capacity (same weight base = 2000W each).
        """
        # Use equal capacity AND equal SoC headroom weights to match spec §7a example
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        states = _make_states(kontor_soc=40.0, forrad_soc=15.0)
        snap = _make_snap(brain_w=-4000.0, eq_threshold=5.0, eq_max_bias=500.0)

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        assert result.equalization_active is True
        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)
        # forrad has lower SoC → gets more charge (more negative)
        # (headroom weighting already gives forrad more base; equalization amplifies this)
        assert result.targets["forrad"] < result.targets["kontor"]
        # Zero-sum preserved
        total = result.targets["kontor"] + result.targets["forrad"]
        assert total == pytest.approx(-4000.0, abs=1.0)

    def test_t1_7_discharge_higher_soc_gets_more(self):
        """T1.7: K=40%, F=15% (div=25%>5%), target=+4000W (discharge) → kontor discharges more.

        Spec §7a: higher-SoC bank (kontor=40%) gets more discharge than lower-SoC (forrad=15%).
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        states = _make_states(kontor_soc=40.0, forrad_soc=15.0)
        snap = _make_snap(brain_w=4000.0, eq_threshold=5.0, eq_max_bias=500.0)

        result = distribute_target_to_banks(4000.0, configs, states, snap)

        assert result.equalization_active is True
        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)
        # kontor has higher SoC → gets more discharge (both headroom-weighting and equalization agree)
        assert abs(result.targets["kontor"]) > abs(result.targets["forrad"])
        # Zero-sum preserved
        total = result.targets["kontor"] + result.targets["forrad"]
        assert total == pytest.approx(4000.0, abs=1.0)

    def test_t1_8_below_threshold_no_equalization(self):
        """T1.8: K=50%, F=47% (div=3%≤5%), target=-4000W (charge) → no bias, equal distribution."""
        configs = _make_configs(kontor_cap=7.0, forrad_cap=7.0)
        states = _make_states(kontor_soc=50.0, forrad_soc=47.0)
        snap = _make_snap(brain_w=-4000.0, eq_threshold=5.0)

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        assert result.equalization_active is False
        # Should be roughly equal (headroom-weighted, both similar SoC + same capacity)
        diff = abs(result.targets["kontor"] - result.targets["forrad"])
        assert diff < 4000.0 * 0.10  # within 10% (headroom differs slightly)

    def test_t1_9_three_banks_zero_sum(self):
        """T1.9: 3 banks A=80% B=50% C=20%, target=-6000W (charge), MAX_BIAS=500W → zero-sum."""
        from custom_components.bat_balancer.models import BankConfig, BankState

        configs = {
            "A": BankConfig(id="A", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0),
            "B": BankConfig(id="B", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0),
            "C": BankConfig(id="C", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0),
        }
        states = {
            "A": BankState(bank_id="A", current_soc=80.0, is_online=True),
            "B": BankState(bank_id="B", current_soc=50.0, is_online=True),
            "C": BankState(bank_id="C", current_soc=20.0, is_online=True),
        }
        snap = _make_snap(
            brain_w=-6000.0, eq_threshold=5.0, eq_max_bias=500.0, eq_full_bias_threshold=100.0
        )

        result = distribute_target_to_banks(-6000.0, configs, states, snap)

        assert result.equalization_active is True
        total = sum(result.targets[k] for k in ["A", "B", "C"])
        assert total == pytest.approx(-6000.0, abs=1.0)  # zero-sum ✓
        # C (lowest SoC=20%) gets most charge (most negative)
        assert result.targets["C"] < result.targets["B"] < 0

    def test_t1_10_bias_bms_cap_conflict(self):
        """T1.10: bias gives forrad all charge but BMS-cap=1800W → cap.
        D5: overflow suppressed (forrad SoC=15% < kontor SoC=40%) → kontor receives no overflow.
        """
        configs = _make_configs(
            kontor_cap=7.0,
            forrad_cap=7.0,
            kontor_max_charge=7000.0,
            forrad_max_charge=1800.0,
        )
        states = _make_states(kontor_soc=40.0, forrad_soc=15.0)  # div=25% → A3 full-bias
        snap = _make_snap(brain_w=-4000.0, eq_threshold=5.0, eq_max_bias=500.0)

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        # Forrad capped at -1800W
        assert result.targets["forrad"] == pytest.approx(-1800.0, abs=0.5)
        # D5: overflow (2200W) suppressed — NOT sent to kontor (higher SoC=40%)
        assert result.targets["kontor"] == pytest.approx(0.0, abs=0.5)
        assert result.actual_total_w == pytest.approx(1800.0, abs=2.0)
        assert result.bms_cap_suppressed_w > 0.0

    def test_equalization_zero_sum_invariant(self):
        """Sum of targets == target_w after equalization (within 1W tolerance).

        Uses BMS caps > target to avoid D5 suppression — tests zero-sum invariant only.
        """
        configs = _make_configs(
            kontor_cap=14.0, forrad_cap=6.0, forrad_max_charge=7000.0, forrad_max_discharge=7000.0
        )
        states = _make_states(kontor_soc=70.0, forrad_soc=20.0)
        snap = _make_snap(brain_w=-3000.0, eq_threshold=5.0, eq_max_bias=500.0)

        result = distribute_target_to_banks(-3000.0, configs, states, snap)

        total = sum(result.targets[k] for k in ["kontor", "forrad"])
        assert total == pytest.approx(-3000.0, abs=1.0)
        assert result.equalization_active is True

    def test_equalization_max_bias_respected(self):
        """PT-BAL-2: raw bias per bank ≤ MAX_BIAS_W even at extreme divergence.

        equalization_bias_max_w in result reports the max raw bias applied.
        The net target difference may differ (e.g., if one bank has zero headroom-weight),
        but the raw bias per bank was clipped to MAX_BIAS_W before application.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        states = _make_states(kontor_soc=0.0, forrad_soc=100.0)
        # eq_full_bias_threshold=101 to stay in progressive-bias path (divergence=100% otherwise triggers full-bias)
        snap = _make_snap(
            brain_w=-4000.0, eq_threshold=5.0, eq_max_bias=500.0, eq_full_bias_threshold=101.0
        )

        result = distribute_target_to_banks(-4000.0, configs, states, snap)

        # The raw bias reported by the engine must be ≤ MAX_BIAS_W (500W)
        assert result.equalization_bias_max_w <= 500.0 + 1.0
        # Zero-sum preserved after re-normalization
        assert result.actual_total_w == pytest.approx(4000.0, abs=1.0)

    def test_tc_bias_discharge_high_soc_gets_more(self):
        """TC-BIAS-DISCHARGE: during discharge, higher-SoC bank gets MORE discharge (BN-2 invariant).

        Uses divergence=12% (inside progressive-bias range, below full-bias threshold of 15%).
        Equal-capacity banks so headroom-weighting gives proportional distribution.
        Equalization must steer MORE discharge to the higher-SoC bank.
        Assertion: abs(targets[high_soc]) > abs(targets[low_soc]) after equalization.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        # kontor SoC=62 (higher), forrad SoC=50 (lower), divergence=12% — in progressive range
        states = _make_states(kontor_soc=62.0, forrad_soc=50.0)
        snap = _make_snap(brain_w=1000.0, eq_threshold=10.0, eq_max_bias=200.0)

        result = distribute_target_to_banks(1000.0, configs, states, snap)

        assert result.equalization_active is True
        # kontor (SoC=62, higher) must discharge MORE than forrad (SoC=50, lower)
        assert abs(result.targets["kontor"]) > abs(result.targets["forrad"]), (
            f"BN-2: kontor={result.targets['kontor']:.1f}W forrad={result.targets['forrad']:.1f}W — "
            "higher-SoC bank must get more discharge"
        )
        # Total must be conserved
        total = abs(result.targets["kontor"]) + abs(result.targets["forrad"])
        assert total == pytest.approx(1000.0, abs=1.0)
        # Both banks should have non-zero contribution (bias=200W, not strong enough to zero one out at 12% gap)
        assert (
            result.targets["forrad"] > 0.0
        ), "forrad should still get some discharge with 200W bias at 12% gap"

    def test_tc_bias_discharge_reversed(self):
        """TC-BIAS-DISCHARGE reversed: forrad higher SoC → forrad discharges more (divergence=12%)."""
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=7.0, max_charge_w=7000.0, max_discharge_w=7000.0
            ),
        }
        # forrad SoC=62 (higher), kontor SoC=50 (lower), divergence=12% — in progressive range
        states = _make_states(kontor_soc=50.0, forrad_soc=62.0)
        snap = _make_snap(brain_w=1000.0, eq_threshold=10.0, eq_max_bias=200.0)

        result = distribute_target_to_banks(1000.0, configs, states, snap)

        assert result.equalization_active is True
        assert (
            abs(result.targets["forrad"]) > abs(result.targets["kontor"])
        ), f"BN-2 reversed: forrad={result.targets['forrad']:.1f}W kontor={result.targets['kontor']:.1f}W"
        assert result.targets["kontor"] > 0.0

    def test_n1_single_bank_no_equalization(self):
        """PT-BAL-5: N=1 online → no equalization possible, full target to that bank."""
        configs = _make_configs()
        states = _make_states(forrad_online=False, kontor_soc=60.0)
        snap = _make_snap(brain_w=-2000.0, eq_threshold=5.0)

        result = distribute_target_to_banks(-2000.0, configs, states, snap)

        assert result.equalization_active is False
        assert result.targets["kontor"] == pytest.approx(-2000.0, abs=2.0)
        assert result.targets["forrad"] == pytest.approx(0.0)

    def test_tc_offer_cap_zero_tolerance(self):
        """TC-OFFER-CAP-ZERO-TOLERANCE (A2): Σ|distribution| ≤ |offer| EXACTLY (0W tolerance).

        With +100W slack removed, brain-offer clamp fires at sigma > offer, not offer+100.
        Verify that offer=3000W cannot yield distribution_sum=3100W.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=14.0, max_charge_w=7589.0, max_discharge_w=7589.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=6.0, max_charge_w=2473.0, max_discharge_w=2473.0
            ),
        }
        states = _make_states(kontor_soc=50.0, forrad_soc=50.0)
        # eq_threshold=100 disables equalization; house_grid high so ZG-12 doesn't fire
        snap = _make_snap(brain_w=-3000.0, eq_threshold=100.0)

        result = distribute_target_to_banks(-3000.0, configs, states, snap)

        assert (
            result.actual_total_w <= 3000.0 + 1e-6
        ), f"A2: distribution_sum={result.actual_total_w:.3f}W > offer=3000W (0-tolerance violated)"
        assert result.actual_total_w == pytest.approx(3000.0, abs=0.01)

    def test_tc_bias_full_override(self):
        """TC-BIAS-FULL-OVERRIDE (A3): SoC kontor=47% förråd=82%, gap=35% > 15% threshold.

        CHARGE offer=+3000W → 100% to kontor (lowest SoC), förråd=0W.
        AC-A-1: dist_kontor=3000, dist_forrad=0.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=15.0, max_charge_w=5000.0, max_discharge_w=5000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=10.0, max_charge_w=3000.0, max_discharge_w=3000.0
            ),
        }
        states = _make_states(kontor_soc=47.0, forrad_soc=82.0)
        snap = _make_snap(
            brain_w=-3000.0,
            eq_threshold=1.0,
            eq_max_bias=1000.0,
            eq_full_bias_threshold=15.0,
        )

        result = distribute_target_to_banks(-3000.0, configs, states, snap)

        assert result.equalization_active is True
        assert result.targets["kontor"] == pytest.approx(
            -3000.0, abs=1.0
        ), f"A3: kontor={result.targets['kontor']:.1f}W expected ~-3000W (100% to lowest SoC)"
        assert result.targets["forrad"] == pytest.approx(
            0.0, abs=1.0
        ), f"A3: forrad={result.targets['forrad']:.1f}W expected 0W"
        assert result.actual_total_w == pytest.approx(3000.0, abs=1.0)

    def test_tc_bias_full_override_discharge(self):
        """TC-BIAS-FULL-OVERRIDE discharge: kontor=47% förråd=82%, gap=35% > 15%.

        DISCHARGE offer=-2000W → 100% to förråd (highest SoC), kontor=0W.
        AC-A-3: discharge goes to highest-SoC bank.
        """
        configs = {
            "kontor": BankConfig(
                id="kontor", capacity_kwh=15.0, max_charge_w=5000.0, max_discharge_w=5000.0
            ),
            "forrad": BankConfig(
                id="forrad", capacity_kwh=10.0, max_charge_w=3000.0, max_discharge_w=3000.0
            ),
        }
        states = _make_states(kontor_soc=47.0, forrad_soc=82.0)
        snap = _make_snap(
            brain_w=2000.0,
            eq_threshold=1.0,
            eq_max_bias=1000.0,
            eq_full_bias_threshold=15.0,
        )

        result = distribute_target_to_banks(2000.0, configs, states, snap)

        assert result.equalization_active is True
        assert result.targets["forrad"] == pytest.approx(
            2000.0, abs=1.0
        ), f"A3: forrad={result.targets['forrad']:.1f}W expected ~2000W (100% to highest SoC)"
        assert result.targets["kontor"] == pytest.approx(
            0.0, abs=1.0
        ), f"A3: kontor={result.targets['kontor']:.1f}W expected 0W"
