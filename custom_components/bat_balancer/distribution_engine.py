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
            targets=dict.fromkeys(all_bank_ids, 0.0),
            actual_total_w=0.0,
            rejected_w=abs(target_w),
            rejected_reason=RejectedReason.NO_AVAILABLE_BANKS,
            status=BatBalancerStatus.ERROR,
        )

    # --- target=0 → STANDBY ---
    if abs(target_w) < _FLOAT_TOL:
        return DistributionResult(
            targets=dict.fromkeys(all_bank_ids, 0.0),
            actual_total_w=0.0,
            status=BatBalancerStatus.OK,
        )

    charging = target_w < 0  # new convention: negative = charge

    # --- 1. Headroom-weighted distribution ---
    weights: dict[str, float] = {}
    for bc in online:
        bs = bank_states.get(bc.id, BankState(bc.id))
        if charging:
            if bs.current_soc >= snapshot.soc_charge_ceiling_pct:
                weights[bc.id] = 0.0
            else:
                weights[bc.id] = max(0.0, (100.0 - bs.current_soc)) * bc.capacity_kwh
        else:
            weights[bc.id] = max(0.0, (bs.current_soc - bc.min_soc_pct)) * bc.capacity_kwh

    total_weight = sum(weights.values()) or 1.0
    targets: dict[str, float] = {bc.id: target_w * weights[bc.id] / total_weight for bc in online}

    # --- 2.5. SoC-equalization bias (INV-24, zero-sum) ---
    equalization_active = False
    equalization_bias_max_w = 0.0

    socs = [bank_states.get(bc.id, BankState(bc.id)).current_soc for bc in online]
    soc_divergence = max(socs) - min(socs)
    threshold = snapshot.soc_equalization_threshold_pct
    max_bias_w = snapshot.soc_equalization_max_bias_w
    full_bias_threshold = snapshot.soc_equalization_full_bias_threshold_pct

    if soc_divergence > threshold and len(online) > 1:
        equalization_active = True

        if soc_divergence >= full_bias_threshold:
            # A3: gap ≥ full_bias_threshold → 100% to one bank
            # charge: lowest SoC bank gets everything; discharge: highest SoC bank gets everything
            if charging:
                winner = min(
                    online, key=lambda bc: bank_states.get(bc.id, BankState(bc.id)).current_soc
                )
            else:
                winner = max(
                    online, key=lambda bc: bank_states.get(bc.id, BankState(bc.id)).current_soc
                )
            for bc in online:
                targets[bc.id] = target_w if bc.id == winner.id else 0.0
            equalization_bias_max_w = abs(target_w)
        else:
            avg_soc = sum(socs) / len(socs)
            biases: dict[str, float] = {}
            for bc in online:
                bs = bank_states.get(bc.id, BankState(bc.id))
                if charging:
                    # Positive bias = charge more. Below-avg SoC → positive.
                    # D1: normalize against full_bias_threshold (not soc_divergence) so bias scales
                    # linearly with gap magnitude (soc_divergence=2pp → small bias, not constant ±max).
                    raw = (avg_soc - bs.current_soc) / full_bias_threshold * max_bias_w
                else:
                    # Positive bias = discharge more. Above-avg SoC → positive.
                    raw = (bs.current_soc - avg_soc) / full_bias_threshold * max_bias_w
                biases[bc.id] = max(-max_bias_w, min(max_bias_w, raw))

            # Zero-sum correction (compensates float drift)
            bias_sum = sum(biases.values())
            correction = bias_sum / len(online)
            if charging:
                # Charge targets are negative — subtract positive bias to make more negative.
                for bc in online:
                    targets[bc.id] -= biases[bc.id] - correction
            else:
                for bc in online:
                    targets[bc.id] += biases[bc.id] - correction

            # Clip direction violations (charging target must stay ≤ 0, discharge ≥ 0)
            if charging:
                for bc in online:
                    targets[bc.id] = min(0.0, targets[bc.id])
            else:
                for bc in online:
                    targets[bc.id] = max(0.0, targets[bc.id])

            # Re-normalize after clip so zero-sum is restored (INV-24 guarantee)
            clipped_total = sum(abs(targets[bc.id]) for bc in online)
            if clipped_total > _FLOAT_TOL and abs(clipped_total - abs(target_w)) > 0.01:
                scale = abs(target_w) / clipped_total
                for bc in online:
                    targets[bc.id] *= scale

            equalization_bias_max_w = max(abs(b) for b in biases.values())

    # --- 2.6. SoC-ceiling re-enforcement (charging only, overrides equalization) ---
    soc_ceil_bank_ids: set[str] = set()
    if charging:
        for bc in online:
            bs = bank_states.get(bc.id, BankState(bc.id))
            if bs.current_soc >= snapshot.soc_charge_ceiling_pct and targets.get(bc.id, 0.0) < 0:
                _LOGGER.debug(
                    "bat_balancer: SoC-ceiling bank=%s soc=%.1f%% → 0W",
                    bc.id,
                    bs.current_soc,
                )
                targets[bc.id] = 0.0
                soc_ceil_bank_ids.add(bc.id)

    # --- 2.7. Fix-A max-asymmetry cap (IT-5677 cross-dir RCA) ---
    # Prevents any bank from receiving more than soc_max_asymmetry_pct of total offer.
    # Ensures all online banks always get a non-zero allocation, preventing autonomous HW behaviour
    # when a bank receives 0W (standby) while Brain has a non-zero offer.
    max_asym_pct = max(0.0, min(100.0, snapshot.soc_max_asymmetry_pct))
    if len(online) > 1 and abs(target_w) > _FLOAT_TOL and max_asym_pct < 100.0:
        max_w_per_bank = abs(target_w) * max_asym_pct / 100.0
        asymmetry_clamped = False
        for bc in online:
            if bc.id in soc_ceil_bank_ids:
                continue  # SoC-ceiling banks are intentionally 0 — don't re-distribute to them
            if abs(targets.get(bc.id, 0.0)) > max_w_per_bank + _FLOAT_TOL:
                excess_w = abs(targets[bc.id]) - max_w_per_bank
                targets[bc.id] = -max_w_per_bank if charging else max_w_per_bank
                # Distribute excess proportionally to remaining banks
                recipients = [b for b in online if b.id != bc.id and b.id not in soc_ceil_bank_ids]
                if recipients:
                    share = excess_w / len(recipients)
                    for r in recipients:
                        targets[r.id] += -share if charging else share
                asymmetry_clamped = True
        if asymmetry_clamped:
            _LOGGER.info(
                "bat_balancer Fix-A: asymmetry clamped to %.0f%% (max=%.0fW/bank) targets=%s",
                max_asym_pct,
                max_w_per_bank,
                {bid: round(targets[bid]) for bid in targets},
            )

    # --- 3. BMS-cap + overflow-redistribution ---
    overflow_redistributed = False
    overflow_w = 0.0
    capped_bank_ids: set[str] = set()
    for bc in online:
        bs = bank_states.get(bc.id, BankState(bc.id))
        cap = _effective_cap(bc, bs, charging)
        signed_target = targets[bc.id]
        magnitude = abs(signed_target)
        if magnitude > cap + _FLOAT_TOL:
            overflow_w += magnitude - cap
            targets[bc.id] = -cap if charging else cap
            capped_bank_ids.add(bc.id)

    bms_cap_suppressed_w = 0.0
    if overflow_w > _FLOAT_TOL:
        overflow_redistributed = True
        # D5: pass capped SoC info when equalization is active — suppress overflow
        # to the bank with higher SoC (charging) / lower SoC (discharge) so we don't
        # actively widen the SoC gap by sending overflow to the wrong bank.
        capped_socs = (
            [bank_states.get(bid, BankState(bid)).current_soc for bid in capped_bank_ids]
            if equalization_active
            else []
        )
        targets, bms_cap_suppressed_w = _redistribute_overflow(
            overflow_w,
            online,
            bank_states,
            targets,
            charging,
            capped_socs,
            soc_charge_ceiling_pct=snapshot.soc_charge_ceiling_pct,
        )

    # --- 4.5. Brain-offer invariant clamp (A2: 0-tolerance, INV-1 exact) ---
    # Sum of |per-bank targets| must not exceed |brain_target_bat_w| exactly.
    _sigma_pre_clamp = sum(abs(targets.get(bc.id, 0.0)) for bc in online)
    _brain_offer_cap_w = abs(target_w)
    if _sigma_pre_clamp > _brain_offer_cap_w + _FLOAT_TOL:
        _scale = _brain_offer_cap_w / _sigma_pre_clamp
        for bc in online:
            targets[bc.id] = targets[bc.id] * _scale
        _LOGGER.warning(
            "bat_balancer: brain-offer clamp — sigma=%.0fW offer=%.0fW scale=%.4f",
            _sigma_pre_clamp,
            abs(target_w),
            _scale,
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

    status = _derive_status(overflow_redistributed, rejected_w)

    return DistributionResult(
        targets=targets,
        actual_total_w=actual_total_w,
        rejected_w=rejected_w,
        rejected_reason=rejected_reason,
        status=status,
        equalization_active=equalization_active,
        equalization_bias_max_w=equalization_bias_max_w,
        inv23_violation=inv23_violation,
        overflow_redistributed=overflow_redistributed,
        capped_bank_ids=frozenset(capped_bank_ids),
        bms_cap_suppressed_w=bms_cap_suppressed_w,
        soc_ceil_bank_ids=frozenset(soc_ceil_bank_ids),
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
    capped_socs: list[float] | None = None,
    soc_charge_ceiling_pct: float = 100.0,
) -> tuple[dict[str, float], float]:
    """Redistribute overflow to banks with remaining headroom (INV-25).

    D5: if capped_socs provided, suppress overflow to banks whose SoC would worsen
    the equalization gap (charging: bank with higher SoC than capped bank; discharge: lower).
    Returns (updated targets, suppressed_w).
    """
    headroom_banks = [
        bc
        for bc in online
        if _has_headroom(
            bc, bank_states.get(bc.id, BankState(bc.id)), targets, charging, soc_charge_ceiling_pct
        )
    ]

    # D5: filter out headroom banks that would worsen SoC balance
    suppressed_w = 0.0
    if capped_socs and headroom_banks:
        if charging:
            min_capped_soc = min(capped_socs)
            d5_allowed = [
                bc
                for bc in headroom_banks
                if bank_states.get(bc.id, BankState(bc.id)).current_soc
                <= min_capped_soc + _FLOAT_TOL
            ]
        else:
            max_capped_soc = max(capped_socs)
            d5_allowed = [
                bc
                for bc in headroom_banks
                if bank_states.get(bc.id, BankState(bc.id)).current_soc
                >= max_capped_soc - _FLOAT_TOL
            ]
        if len(d5_allowed) < len(headroom_banks):
            suppressed_w = overflow_w  # conservative: mark all as suppressed
            _LOGGER.info(
                "bat_balancer D5: suppressing %.0fW overflow to higher-SoC bank "
                "(capped_soc=%.1f%%) — protecting SoC balance",
                overflow_w,
                min(capped_socs) if charging else max(capped_socs),
            )
        headroom_banks = d5_allowed

    if not headroom_banks:
        _LOGGER.debug(
            "bat_balancer: overflow %.0fW cannot be redistributed — all banks capped", overflow_w
        )
        return targets, suppressed_w

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
            targets[bc.id] = max(
                targets[bc.id] - share,
                -_effective_cap(bc, bank_states.get(bc.id, BankState(bc.id)), True),
            )
        else:
            targets[bc.id] = min(
                targets[bc.id] + share,
                _effective_cap(bc, bank_states.get(bc.id, BankState(bc.id)), False),
            )

    return targets, suppressed_w


def _has_headroom(
    bc: BankConfig,
    bs: BankState,
    targets: dict[str, float],
    charging: bool,
    soc_charge_ceiling_pct: float = 100.0,
) -> bool:
    """Return True if bank still has capacity to absorb more power."""
    if charging and bs.current_soc >= soc_charge_ceiling_pct:
        return False
    cap = _effective_cap(bc, bs, charging)
    return abs(targets.get(bc.id, 0.0)) < cap - _FLOAT_TOL


def _derive_status(
    overflow_redistributed: bool,
    rejected_w: float,
) -> BatBalancerStatus:
    if rejected_w > 1.0:
        return BatBalancerStatus.OVERFLOW_REDISTRIBUTED
    if overflow_redistributed:
        return BatBalancerStatus.OVERFLOW_REDISTRIBUTED
    return BatBalancerStatus.OK
