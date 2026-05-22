"""IT-2104 W4+W5 regression tests.

Design B: pure-slave enforce — coordinator clamps target_effective to actual Brain offer.
Design D: cross-direction guard — if banks would operate in opposing directions → ALL STANDBY.

Hard invariants under test:
  - offer=0 → distribution=0,0 (100 ticks, no drift)
  - distribution_sum <= |offer| always
  - NO bank combination where one charges and another discharges simultaneously
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    BANKS,
    ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W,
    ENTITY_BRAIN_TARGET_BAT_W,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_coord(
    brain_offer_w: float = 0.0, target_effective_w: float = 0.0
) -> BatBalancerCoordinator:
    from custom_components.bat_balancer.models import BankConfig, BatBalancerState
    from custom_components.bat_balancer.sign_state_machine import SignStateMachine

    hass = MagicMock()
    hass.data = {}
    hass.services.async_call = AsyncMock()

    def _state_get(entity_id):
        s = MagicMock()
        if entity_id == ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W:
            s.state = str(target_effective_w)
        elif entity_id == ENTITY_BRAIN_TARGET_BAT_W:
            s.state = str(brain_offer_w)
        elif entity_id.startswith("input_number.") and "soc" in entity_id:
            return None  # ceiling/threshold helpers → use defaults
        elif "soc" in entity_id or "bat_soc" in entity_id:
            s.state = "60"
        elif "charge_max" in entity_id or "discharge_max" in entity_id:
            s.state = "7000"
        elif "battery_mode" in entity_id:
            s.state = "battery_standby"
        elif "shadow" in entity_id:
            s.state = "off"
        elif "bat_balancer_mode" in entity_id:
            s.state = "AUTO"
        elif "cycle_seconds" in entity_id:
            s.state = "5"
        elif entity_id in ("input_boolean.bat_balancer_allow_hw_autonomy",):
            s.state = "off"
        else:
            return None  # unknown entity → coordinator uses default constant
        return s

    hass.states.get = MagicMock(side_effect=_state_get)

    entry = MagicMock()
    entry.entry_id = "test"

    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._entry = entry
    coord._bank_configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
    coord._state = BatBalancerState()
    coord._last_brain_write_ts = time.monotonic()
    coord._bank_was_online = {bid: True for bid in BANKS}
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._last_written_ems_modes = {bid: None for bid in BANKS}
    coord._hw_mismatch_ticks = {bid: 0 for bid in BANKS}
    coord._full_bias_active = False
    coord._last_ems_limit_w = {bid: 0.0 for bid in BANKS}
    coord._current_emergency_active = False
    coord._current_below_reset_since = None
    coord._hw_autonomy_initialized = True
    coord._hw_actual_w = {bid: 0.0 for bid in BANKS}
    coord._hw_overshoot_ema = 0.0
    coord._hw_overshoot_ticks = 0
    coord._hw_undershoot_ticks = 0
    coord._hw_correction_active = False
    return coord


# ---------------------------------------------------------------------------
# Design B: pure-slave clamp tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_offer_zero_target_nonzero_clamped_to_zero() -> None:
    """W4 scenario: Brain offer=0, target_effective=+2137W → coordinator clamps to 0W → no distribution."""
    coord = _make_coord(brain_offer_w=0.0, target_effective_w=2137.0)

    await coord._tick()

    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    assert k == 0.0, f"kontor={k}: should be 0W (clamped to brain_offer=0)"
    assert f == 0.0, f"forrad={f}: should be 0W (clamped to brain_offer=0)"


@pytest.mark.asyncio
async def test_b_offer_zero_no_drift_100_ticks() -> None:
    """Pure-slave regression: offer=0 → distribution=0,0 across 100 ticks — no autonomous drift."""
    coord = _make_coord(brain_offer_w=0.0, target_effective_w=2137.0)

    for tick in range(100):
        await coord._tick()
        k = coord._last_ems_limit_w["kontor"]
        f = coord._last_ems_limit_w["forrad"]
        assert k == 0.0, f"tick {tick}: kontor={k} (drift)"
        assert f == 0.0, f"tick {tick}: forrad={f} (drift)"


@pytest.mark.asyncio
async def test_b_offer_matches_target_no_clamp() -> None:
    """Within tolerance but target_effective > brain_offer → hard cap to brain_offer (pure-slave invariant)."""
    offer = -5000.0
    target = (
        -5050.0
    )  # diff=50 < BRAIN_OFFER_TOLERANCE_W=100 → tolerance clamp skipped, hard cap applies
    coord = _make_coord(brain_offer_w=offer, target_effective_w=target)

    await coord._tick()

    total = abs(coord._last_ems_limit_w["kontor"]) + abs(coord._last_ems_limit_w["forrad"])
    assert total > 0.0, "distribution should proceed"
    # Hard cap: distribution must never exceed brain_offer, even within tolerance window.
    assert total <= abs(offer) + 1.0, f"distribution {total} > brain_offer {abs(offer)}"


@pytest.mark.asyncio
async def test_b_offer_nonzero_target_diverges_clamped() -> None:
    """When target_effective diverges from brain_offer by > tolerance → clamp to brain_offer."""
    # Simulate sign-convention bug: brain=-5000 (charge) but auto_w fallback gives +3000 (wrong)
    coord = _make_coord(brain_offer_w=-5000.0, target_effective_w=3000.0)

    await coord._tick()

    # After clamp to -5000: distribution should be charge (negative values)
    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    # Both should be charge (negative in signed convention: neg=charge)
    assert k < 0.0 or f < 0.0, f"after clamp to -5000: expect charge, got kontor={k} forrad={f}"
    total = abs(k) + abs(f)
    assert total <= 5001.0, f"distribution {total} exceeds clamped offer 5000"


@pytest.mark.asyncio
async def test_b_distribution_sum_never_exceeds_offer() -> None:
    """For all offer values: sum(|distribution|) <= |offer| + 1W tolerance."""
    for offer_w in [-8000.0, -3000.0, -500.0, 0.0, 500.0, 3000.0, 8000.0]:
        coord = _make_coord(brain_offer_w=offer_w, target_effective_w=offer_w)
        await coord._tick()
        total = abs(coord._last_ems_limit_w["kontor"]) + abs(coord._last_ems_limit_w["forrad"])
        assert (
            total <= abs(offer_w) + 1.0
        ), f"offer={offer_w}: distribution {total} > |offer| {abs(offer_w)}"


# ---------------------------------------------------------------------------
# Design D: cross-direction guard tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_cross_direction_triggers_all_standby() -> None:
    """D: If sign machine emits opposing directions → both banks written STANDBY."""
    coord = _make_coord(brain_offer_w=3000.0, target_effective_w=3000.0)

    # Manually corrupt sign machines to simulate cross-direction scenario:
    # kontor in DISCHARGE state, forrad in CHARGE state
    from custom_components.bat_balancer.sign_state_machine import SignStateMachine

    coord._sign_machines["kontor"] = SignStateMachine()
    coord._sign_machines["forrad"] = SignStateMachine()
    # Prime kontor for discharge direction
    coord._sign_machines["kontor"].tick(3000.0)
    # Prime forrad for charge direction (simulate state machine bug)
    coord._sign_machines["forrad"].tick(-3000.0)

    # Reset ems_mode caches
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}

    # Set target so that ticked values will be opposing (3000 kontor, -3000 forrad)
    # We directly test _tick with an offer that would produce opposing per-bank targets
    # by pre-seeding last distribution: use a direct cross-direction target pair
    # Actually, to directly test Design D, we'll mock distribute_target_to_banks
    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    cross_result = DistributionResult(
        targets={"kontor": 3000.0, "forrad": -3000.0},  # CROSS: one discharge, one charge
        actual_total_w=6000.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=cross_result,
    ):
        # Reset sign machines to plain state so ticked values match raw targets
        coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
        await coord._tick()

    # Verify: both banks written to STANDBY (0W)
    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    assert k == 0.0, f"cross-direction guard: kontor should be 0W STANDBY, got {k}"
    assert f == 0.0, f"cross-direction guard: forrad should be 0W STANDBY, got {f}"

    # Verify: notify was called
    notify_calls = [
        c
        for c in coord.hass.services.async_call.call_args_list
        if c.args[0] == "notify" and c.args[1] == "mobile_app_bmq_iphone"
    ]
    assert len(notify_calls) == 1, "cross-direction must send P0 notification"
    assert "CROSS-DIRECTION" in notify_calls[0].args[2]["title"]


@pytest.mark.asyncio
async def test_d_no_false_positive_both_discharge() -> None:
    """D: Both banks discharging → NOT cross-direction → write proceeds normally."""
    coord = _make_coord(brain_offer_w=3000.0, target_effective_w=3000.0)

    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    same_dir_result = DistributionResult(
        targets={"kontor": 1800.0, "forrad": 1200.0},  # both discharge
        actual_total_w=3000.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=same_dir_result,
    ):
        await coord._tick()

    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    assert k > 0.0, f"kontor should be discharging, got {k}"
    assert f > 0.0, f"forrad should be discharging, got {f}"


@pytest.mark.asyncio
async def test_d_no_false_positive_one_standby_one_discharge() -> None:
    """D: One bank standby (0W), other discharging → NOT cross-direction (IT-2102 allows this)."""
    coord = _make_coord(brain_offer_w=3000.0, target_effective_w=3000.0)

    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    partial_result = DistributionResult(
        targets={"kontor": 3000.0, "forrad": 0.0},  # forrad standby, kontor discharge
        actual_total_w=3000.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=partial_result,
    ):
        await coord._tick()

    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    assert k > 0.0, f"kontor should be discharging, got {k}"
    assert f == 0.0, f"forrad should be 0W (standby), got {f}"


# ---------------------------------------------------------------------------
# IT-2104 Q3: soc_ceil reason reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_q3_soc_ceil_reason_all_banks() -> None:
    """Q3: Both banks at SoC ceiling → reason = soc_ceil, per-bank = soc_ceil."""
    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    coord = _make_coord(brain_offer_w=-5000.0, target_effective_w=-5000.0)

    ceil_result = DistributionResult(
        targets={"kontor": 0.0, "forrad": 0.0},
        actual_total_w=0.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
        soc_ceil_bank_ids=frozenset({"kontor", "forrad"}),
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=ceil_result,
    ):
        await coord._tick()

    assert coord._reason == "soc_ceil", f"expected soc_ceil, got {coord._reason}"
    assert coord._bank_reasons.get("kontor") == "soc_ceil"
    assert coord._bank_reasons.get("forrad") == "soc_ceil"


@pytest.mark.asyncio
async def test_q3_soc_ceil_reason_one_bank() -> None:
    """Q3: One bank at ceiling, other charging → reason = soc_ceil, bank reason per-bank."""
    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    coord = _make_coord(brain_offer_w=-5000.0, target_effective_w=-5000.0)

    partial_ceil_result = DistributionResult(
        targets={"kontor": 0.0, "forrad": -5000.0},
        actual_total_w=5000.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
        soc_ceil_bank_ids=frozenset({"kontor"}),
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=partial_ceil_result,
    ):
        await coord._tick()

    assert coord._reason == "soc_ceil", f"expected soc_ceil, got {coord._reason}"
    assert coord._bank_reasons.get("kontor") == "soc_ceil"
    assert coord._bank_reasons.get("forrad") == "ok"


@pytest.mark.asyncio
async def test_d_both_charge_no_cross_direction() -> None:
    """D: Both banks charging (negative targets) → NOT cross-direction."""
    coord = _make_coord(brain_offer_w=-5000.0, target_effective_w=-5000.0)

    from unittest.mock import patch

    from custom_components.bat_balancer.const import BatBalancerStatus
    from custom_components.bat_balancer.models import DistributionResult

    charge_result = DistributionResult(
        targets={"kontor": -3000.0, "forrad": -2000.0},  # both charge
        actual_total_w=5000.0,
        status=BatBalancerStatus.OK,
        equalization_active=False,
        equalization_bias_max_w=0.0,
    )

    with patch(
        "custom_components.bat_balancer.coordinator.distribute_target_to_banks",
        return_value=charge_result,
    ):
        await coord._tick()

    k = coord._last_ems_limit_w["kontor"]
    f = coord._last_ems_limit_w["forrad"]
    # Both should be charge (negative signed convention)
    assert k < 0.0, f"kontor should be charging (negative), got {k}"
    assert f < 0.0, f"forrad should be charging (negative), got {f}"
