"""Distribution engine — core of bat_balancer (spec BAL-10-BAT §7 + §7a).

Pure function — zero HA dependency. 100% pytest-bar.

Implements:
  1. Online-filter
  2. Headroom-weighted distribution
  2.5. SoC-equalization bias (INV-24, zero-sum)
  3. BMS-cap + overflow-redistribution
  4. ZG-12 anti-export cap (DISCHARGE only)
  5. Offline banks → 0
  6. INV-23 post-cap check (WARNING only — BMS is force majeure)
  7. Rejected-W reporting
"""

from __future__ import annotations

import logging
import math

from .const import (
    INV23_TOLERANCE_FRACTION,
    BatBalancerStatus,
    RejectedReason,
)
from .models import BankConfig, BankState, DistributionResult, SensorSnapshot

_LOGGER = logging.getLogger(__name__)

_FLOAT_TOL = 1e-6


def distribute_target_to_banks(
    target_w: float,
    bank_configs: dict[str, BankConfig],
    bank_states: dict[str, BankState],
    snapshot: SensorSnapshot,
) -> DistributionResult:
    """Distribute signed target_w across N battery banks.

    target_w: signed — positive=charge, negative=discharge
    Returns DistributionResult with per-bank targets (signed) and metadata.
    """
    all_bank_ids = list(bank_configs.keys())
    online = [
        bank_configs[bid] for bid in all_bank_ids if bank_states.get(bid, BankState(bid)).is_online
    ]

    # --- N=0 guard ---
    if not online:
        _LOGGER.warning(
            "bat_balancer: N=0 online banks — STANDBY all, rejected_w=%s", abs(target_w)
        )
        return DistributionResult(
            targets={bid: 0.0 for bid in all_bank_ids},
            actual_total_w=0.0,
            rejected_w=abs(target_w),
            rejected_reason=RejectedReason.NO_AVAILABLE_BANKS,
            status=BatBalancerStatus.ERROR,
        )

    # --- target=0 → STANDBY ---
    if abs(target_w) < _FLOAT_TOL:
        return DistributionResult(
            targets={bid: 0.0 for bid in all_bank_ids},
            actual_total_w=0.0,
            status=BatBalancerStatus.OK,
        )

    charging = target_w > 0

    # --- 1. Headroom-weighted distribution ---
    weights: dict[str, float] = {}
    for bc in online:
        bs = bank_states.get(bc.id, BankState(bc.id))
        if charging:
            w = max(0.0, (100.0 - bs.current_soc)) * bc.capacity_kwh
        else:
            w = max(0.0, (bs.current_soc - bc.min_soc_pct)) * bc.capacity_kwh
        weights[bc.id] = w

    total_weight = sum(weights.values()) or 1.0
    targets: dict[str, float] = {bc.id: target_w * weights[bc.id] / total_weight for bc in online}

    # --- 2.5. SoC-equalization bias (INV-24, zero-sum) ---
    equalization_active = False
    equalization_bias_max_w = 0.0

    socs = [bank_states.get(bc.id, BankState(bc.id)).current_soc for bc in online]
    soc_divergence = max(socs) - min(socs)
    threshold = snapshot.soc_equalization_threshold_pct
    max_bias_w = snapshot.soc_equalization_max_bias_w

    if soc_divergence > threshold and len(online) > 1:
        equalization_active = True
        avg_soc = sum(socs) / len(socs)
        biases: dict[str, float] = {}
        for bc in online:
            bs = bank_states.get(bc.id, BankState(bc.id))
            if charging:
                raw = (avg_soc - bs.current_soc) / soc_divergence * max_bias_w
            else:
                raw = (bs.current_soc - avg_soc) / soc_divergence * max_bias_w
            biases[bc.id] = max(-max_bias_w, min(max_bias_w, raw))

        # Zero-sum correction (compensates float drift)
        bias_sum = sum(biases.values())
        correction = bias_sum / len(online)
        for bc in online:
            targets[bc.id] += biases[bc.id] - correction

        # Clip direction violations (charging target must stay ≥ 0, discharge ≤ 0)
        if charging:
            for bc in online:
                targets[bc.id] = max(0.0, targets[bc.id])
        else:
            for bc in online:
                targets[bc.id] = min(0.0, targets[bc.id])

        # Re-normalize after clip so zero-sum is restored (INV-24 guarantee)
        clipped_total = sum(abs(targets[bc.id]) for bc in online)
        if clipped_total > _FLOAT_TOL and abs(clipped_total - abs(target_w)) > 0.01:
            scale = abs(target_w) / clipped_total
            for bc in online:
                targets[bc.id] *= scale

        equalization_bias_max_w = max(abs(b) for b in biases.values())

    # --- 3. BMS-cap + overflow-redistribution ---
    overflow_redistributed = False
    overflow_w = 0.0
    for bc in online:
        bs = bank_states.get(bc.id, BankState(bc.id))
        cap = _effective_cap(bc, bs, charging)
        signed_target = targets[bc.id]
        magnitude = abs(signed_target)
        if magnitude > cap + _FLOAT_TOL:
            overflow_w += magnitude - cap
            targets[bc.id] = cap if charging else -cap

    if overflow_w > _FLOAT_TOL:
        overflow_redistributed = True
        targets = _redistribute_overflow(overflow_w, online, bank_states, targets, charging)

    # --- 4. ZG-12 anti-export cap (DISCHARGE only) ---
    zg12_engaged = False
    if not charging:
        sigma = sum(abs(targets.get(bc.id, 0.0)) for bc in online)
        house_deficit = _house_deficit_w(snapshot)
        if sigma > house_deficit + _FLOAT_TOL and house_deficit >= 0:
            scale = house_deficit / sigma if sigma > _FLOAT_TOL else 0.0
            for bc in online:
                targets[bc.id] = targets[bc.id] * scale
            zg12_engaged = True
            _LOGGER.debug(
                "bat_balancer: ZG-12 cap — sigma=%.0fW deficit=%.0fW scale=%.3f",
                sigma,
                house_deficit,
                scale,
            )

    # --- 5. Offline banks → 0 ---
    for bid in all_bank_ids:
        if bid not in targets:
            targets[bid] = 0.0
        bs = bank_states.get(bid, BankState(bid))
        if not bs.is_online:
            targets[bid] = 0.0

    # --- 6. INV-23 post-cap check (WARNING — BMS is force majeure) ---
    online_targets = [abs(targets[bc.id]) for bc in online]
    inv23_violation = False
    if online_targets and abs(target_w) > _FLOAT_TOL:
        diff_pct = (max(online_targets) - min(online_targets)) / abs(target_w)
        if diff_pct > INV23_TOLERANCE_FRACTION:
            inv23_violation = True
            _LOGGER.warning(
                "bat_balancer: INV-23 %.1f%% diff post-cap (BMS-constraint — expected)",
                diff_pct * 100,
            )

    # --- 7. Rejected reporting ---
    actual_total_w = sum(abs(targets[bc.id]) for bc in online)
    rejected_w = max(0.0, abs(target_w) - actual_total_w)
    rejected_reason: RejectedReason | None = None

    if rejected_w > _FLOAT_TOL:
        rejected_reason = RejectedReason.BMS_CAP_AGGREGATE
    elif zg12_engaged:
        rejected_reason = RejectedReason.ZG12_CAP

    status = _derive_status(zg12_engaged, overflow_redistributed, rejected_w)

    return DistributionResult(
        targets=targets,
        actual_total_w=actual_total_w,
        rejected_w=rejected_w,
        rejected_reason=rejected_reason,
        status=status,
        zg12_engaged=zg12_engaged,
        equalization_active=equalization_active,
        equalization_bias_max_w=equalization_bias_max_w,
        inv23_violation=inv23_violation,
        overflow_redistributed=overflow_redistributed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _effective_cap(bc: BankConfig, bs: BankState, charging: bool) -> float:
    """Return effective BMS cap for this bank and direction."""
    if charging:
        dynamic = bs.bms_max_charge_w
        static = bc.max_charge_w
    else:
        dynamic = bs.bms_max_discharge_w
        static = bc.max_discharge_w
    return dynamic if (dynamic is not None and not bs.sensor_stale) else static


def _redistribute_overflow(
    overflow_w: float,
    online: list[BankConfig],
    bank_states: dict[str, BankState],
    targets: dict[str, float],
    charging: bool,
) -> dict[str, float]:
    """Redistribute overflow to banks with remaining headroom (INV-25)."""
    headroom_banks = [
        bc
        for bc in online
        if _has_headroom(bc, bank_states.get(bc.id, BankState(bc.id)), targets, charging)
    ]
    if not headroom_banks:
        _LOGGER.debug(
            "bat_balancer: overflow %.0fW cannot be redistributed — all banks capped", overflow_w
        )
        return targets

    # Distribute overflow proportional to remaining headroom
    remaining_headroom: dict[str, float] = {}
    for bc in headroom_banks:
        bs = bank_states.get(bc.id, BankState(bc.id))
        cap = _effective_cap(bc, bs, charging)
        remaining_headroom[bc.id] = cap - abs(targets[bc.id])

    total_headroom = sum(remaining_headroom.values()) or 1.0
    for bc in headroom_banks:
        share = overflow_w * remaining_headroom[bc.id] / total_headroom
        if charging:
            targets[bc.id] = min(
                targets[bc.id] + share,
                _effective_cap(bc, bank_states.get(bc.id, BankState(bc.id)), True),
            )
        else:
            targets[bc.id] = max(
                targets[bc.id] - share,
                -_effective_cap(bc, bank_states.get(bc.id, BankState(bc.id)), False),
            )

    return targets


def _has_headroom(
    bc: BankConfig,
    bs: BankState,
    targets: dict[str, float],
    charging: bool,
) -> bool:
    """Return True if bank still has capacity to absorb more power."""
    cap = _effective_cap(bc, bs, charging)
    return abs(targets.get(bc.id, 0.0)) < cap - _FLOAT_TOL


def _house_deficit_w(snapshot: SensorSnapshot) -> float:
    """Return max safe discharge W = house_grid_import (positive = deficit).

    If unavailable → bat_max_discharge_total (worst-case, spec §8).
    """
    grid = snapshot.house_grid_w
    if math.isnan(grid):
        from .const import FORRAD_MAX_DISCHARGE_W, KONTOR_MAX_DISCHARGE_W

        return KONTOR_MAX_DISCHARGE_W + FORRAD_MAX_DISCHARGE_W
    # house_grid_w positive = importing from grid → we can discharge up to that
    return max(0.0, grid)


def _derive_status(
    zg12_engaged: bool,
    overflow_redistributed: bool,
    rejected_w: float,
) -> BatBalancerStatus:
    if rejected_w > 1.0:
        if zg12_engaged:
            return BatBalancerStatus.ZG12_CAPPED
        return BatBalancerStatus.OVERFLOW_REDISTRIBUTED
    if zg12_engaged:
        return BatBalancerStatus.ZG12_CAPPED
    if overflow_redistributed:
        return BatBalancerStatus.OVERFLOW_REDISTRIBUTED
    return BatBalancerStatus.OK
