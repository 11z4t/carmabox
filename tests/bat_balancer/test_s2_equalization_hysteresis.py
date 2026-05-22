"""S2 — Equalization full-bias hysteresis prevents ems_limit oscillation.

Root cause: soc_divergence jitters near full_bias_threshold (15%) → distribution
alternates between proportional (~518W) and full-bias (~1134W) every tick.

Fix: once in full-bias, only exit when divergence drops 5% below threshold.
Entry threshold: 15%, exit threshold: 10%.

TC-S2-ENTERS_FULL_BIAS:   divergence=15% → enters full-bias
TC-S2-STAYS_IN_FULL_BIAS: divergence drops to 14% (above exit=10%) → stays full-bias
TC-S2-EXITS_FULL_BIAS:    divergence drops to 9% (below exit=10%) → exits full-bias
TC-S2-COLD_START:         divergence=14% on first tick → proportional (no hysteresis yet)
TC-S2-STABLE_TARGET:      at stable target, divergence near threshold → no oscillation
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    SOC_EQ_FULL_BIAS_HYSTERESIS_PCT,
    SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT,
    SOC_EQ_MAX_BIAS_DEFAULT_W,
    SOC_EQ_THRESHOLD_DEFAULT_PCT,
)
from custom_components.bat_balancer.distribution_engine import distribute_target_to_banks
from custom_components.bat_balancer.models import (
    BankConfig,
    BankState,
    SensorSnapshot,
)

_ENTRY_THRESHOLD = SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT  # 15.0
_EXIT_THRESHOLD = _ENTRY_THRESHOLD - SOC_EQ_FULL_BIAS_HYSTERESIS_PCT  # 10.0
_TARGET_W = -1134.0  # negative = charge
_K_CONFIG = BankConfig.default_kontor()
_F_CONFIG = BankConfig.default_forrad()
_BANK_CONFIGS = {"kontor": _K_CONFIG, "forrad": _F_CONFIG}


def _snapshot(soc_divergence_pct: float, full_bias_threshold: float) -> SensorSnapshot:
    """Build snapshot with requested SoC divergence and effective full_bias_threshold."""
    return SensorSnapshot(
        brain_target_bat_w=_TARGET_W,
        soc_equalization_threshold_pct=SOC_EQ_THRESHOLD_DEFAULT_PCT,
        soc_equalization_max_bias_w=SOC_EQ_MAX_BIAS_DEFAULT_W,
        soc_equalization_full_bias_threshold_pct=full_bias_threshold,
    )


def _bank_states(soc_divergence_pct: float) -> dict[str, BankState]:
    """Return bank states with soc divergence of exactly soc_divergence_pct."""
    soc_k = 50.0 + soc_divergence_pct / 2
    soc_f = 50.0 - soc_divergence_pct / 2
    return {
        "kontor": BankState("kontor", current_soc=soc_k, is_online=True),
        "forrad": BankState("forrad", current_soc=soc_f, is_online=True),
    }


# ---------------------------------------------------------------------------
# TC-S2-ENTERS_FULL_BIAS
# ---------------------------------------------------------------------------


def test_enters_full_bias_at_threshold() -> None:
    """divergence=15% exactly → full-bias should activate (entry threshold)."""
    states = _bank_states(15.0)
    snap = _snapshot(15.0, _ENTRY_THRESHOLD)  # cold-start: use raw threshold
    result = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states, snap)

    assert result.equalization_active
    # In full-bias, one bank gets everything; bias_max_w = abs(target_w)
    assert (
        abs(result.equalization_bias_max_w - abs(_TARGET_W)) < 1.0
    ), f"Expected full-bias (bias_max_w ≈ {abs(_TARGET_W)}), got {result.equalization_bias_max_w}"


# ---------------------------------------------------------------------------
# TC-S2-STAYS_IN_FULL_BIAS (hysteresis: divergence=14% above exit threshold=10%)
# ---------------------------------------------------------------------------


def test_stays_in_full_bias_with_hysteresis() -> None:
    """After entering full-bias, divergence=14% → stays full-bias with exit threshold=10%."""
    states = _bank_states(14.0)
    # Simulate already-in-full-bias: effective threshold = 10% (entry 15% - hysteresis 5%)
    snap = _snapshot(14.0, _EXIT_THRESHOLD)
    result = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states, snap)

    assert result.equalization_active
    assert (
        abs(result.equalization_bias_max_w - abs(_TARGET_W)) < 1.0
    ), f"Should stay in full-bias at 14% divergence with exit threshold {_EXIT_THRESHOLD}%"


# ---------------------------------------------------------------------------
# TC-S2-COLD_START (divergence=14% on first tick → proportional, not full-bias)
# ---------------------------------------------------------------------------


def test_cold_start_no_hysteresis() -> None:
    """First tick: divergence=14% below entry threshold=15% → proportional (no full-bias)."""
    states = _bank_states(14.0)
    snap = _snapshot(14.0, _ENTRY_THRESHOLD)  # cold-start: raw threshold
    result = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states, snap)

    # Should NOT be full-bias (bias_max_w < abs(target_w))
    if result.equalization_active:
        assert (
            abs(result.equalization_bias_max_w - abs(_TARGET_W)) > 1.0
        ), f"At 14% divergence with entry threshold {_ENTRY_THRESHOLD}%, should NOT be full-bias"


# ---------------------------------------------------------------------------
# TC-S2-EXITS_FULL_BIAS (divergence=9% drops below exit threshold=10%)
# ---------------------------------------------------------------------------


def test_exits_full_bias_below_exit_threshold() -> None:
    """Once in full-bias, divergence=9% → exits full-bias (below exit threshold=10%)."""
    states = _bank_states(9.0)
    # Still using exit threshold (simulating we're in full-bias mode)
    snap = _snapshot(9.0, _EXIT_THRESHOLD)
    result = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states, snap)

    # At 9% divergence, even exit threshold (10%) not reached → proportional or inactive
    if result.equalization_active:
        assert (
            abs(result.equalization_bias_max_w - abs(_TARGET_W)) > 1.0
        ), "At 9% divergence with exit threshold 10%, should NOT be in full-bias"


# ---------------------------------------------------------------------------
# TC-S2-STABLE_TARGET: simulate 2 ticks near threshold — no oscillation
# ---------------------------------------------------------------------------


def test_no_oscillation_near_threshold() -> None:
    """Simulate jitter: tick1 divergence=15.5% (full-bias), tick2 divergence=14.5% (still full-bias with hysteresis)."""
    # Tick 1: divergence=15.5% → enters full-bias
    states1 = _bank_states(15.5)
    snap1 = _snapshot(15.5, _ENTRY_THRESHOLD)
    result1 = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states1, snap1)
    assert abs(result1.equalization_bias_max_w - abs(_TARGET_W)) < 1.0, "Tick1: must be full-bias"

    # Tick 2: divergence=14.5% (jitter below entry, but above exit with hysteresis)
    # Coordinator sets effective_threshold = exit_threshold = 10%
    states2 = _bank_states(14.5)
    snap2 = _snapshot(14.5, _EXIT_THRESHOLD)
    result2 = distribute_target_to_banks(_TARGET_W, _BANK_CONFIGS, states2, snap2)

    # With exit threshold=10%, 14.5% still in full-bias → no oscillation
    assert (
        abs(result2.equalization_bias_max_w - abs(_TARGET_W)) < 1.0
    ), "Tick2: with hysteresis, 14.5% divergence must stay in full-bias (no oscillation)"

    # Both ticks → same kontor target (full-bias, charge: lowest SoC gets all = forrad)
    for result in (result1, result2):
        targets = result.targets
        # forrad has lower SoC → gets all charge (full-bias)
        assert abs(targets["forrad"]) > abs(
            targets["kontor"]
        ), "Full-bias charge: lower-SoC bank (forrad) must get majority"
