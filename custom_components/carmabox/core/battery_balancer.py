"""Battery Balancer — proportionell urladdning/laddning.

Pure Python. No HA imports. Fully testable.

Ensures all batteries reach min_soc SIMULTANEOUSLY by distributing
discharge proportionally to available energy (kWh above min_soc).

Cold-lock aware: if a battery's cell temp < threshold, its effective
min_soc is raised (15% → 20%). Discharge still works at cold temps
but min_soc floor is higher to prevent BMS lockout.

Key formula:
    available_i = (soc_i - effective_min_soc_i) / 100 x cap_i
    share_i = available_i / Σ(available_j)
    watts_i = total_watts x share_i
"""

from __future__ import annotations

from dataclasses import dataclass

# EXP-07: Cold-lock discharge blocking (LFP safety)
COLD_DISCHARGE_BLOCK_C = 0.0  # Below 0°C: block discharge entirely
COLD_DISCHARGE_DERATING_C = 4.0  # 0-4°C: reduce discharge by 50%

# EXP-06: SoH derating thresholds
SOH_SEVERE_THRESHOLD_PCT = 70.0  # Below → heavy derating
SOH_MODERATE_THRESHOLD_PCT = 80.0  # Below → moderate derating
SOH_SEVERE_DERATING_PCT = 10.0  # Add to min_soc when SoH < severe
SOH_MODERATE_DERATING_PCT = 5.0  # Add to min_soc when SoH < moderate


@dataclass
class BatteryInfo:
    """Battery state for balancing calculations."""

    id: str
    soc: float  # Current SoC (%)
    cap_kwh: float  # Total capacity (kWh)
    cell_temp_c: float  # Min cell temperature (°C)
    power_w: float = 0.0  # Current power (+ discharge, - charge)
    min_soc: float = 15.0  # Normal min SoC (%)
    min_soc_cold: float = 20.0  # Min SoC when cold (%)
    cold_temp_c: float = 4.0  # Below → use min_soc_cold
    max_discharge_w: float = 5000.0  # Per-battery max discharge (BMS limit)
    max_charge_w: float = 5000.0  # Per-battery max charge (BMS limit, cold-reduced)
    soh_pct: float = 100.0  # State of Health (%)


@dataclass
class BalancerResult:
    """Discharge/charge allocation per battery."""

    allocations: list[BatteryAllocation]
    total_w: int
    balanced: bool  # True if all batteries converge to min_soc together


@dataclass
class BatteryAllocation:
    """Allocation for one battery."""

    id: str
    watts: int
    share_pct: float  # Percentage of total
    _available_kwh: float  # Energy above min_soc (PLAT-1168: private backing field)
    effective_min_soc: float
    at_min_soc: bool  # True if already at or below min_soc

    @property
    def available_kwh(self) -> float:
        """Energy above effective min_soc (kWh)."""
        return self._available_kwh


def effective_min_soc(bat: BatteryInfo) -> float:
    """Calculate effective min SoC based on temperature and SoH.

    SoH derating prevents over-discharge of aged batteries:
      - soh < 70%: add 10% to min_soc
      - soh < 80%: add 5% to min_soc
    Cold derating and SoH derating are cumulative.
    """
    base = bat.min_soc_cold if bat.cell_temp_c < bat.cold_temp_c else bat.min_soc

    # EXP-06: SoH derating — aged cells need higher floor
    if bat.soh_pct < SOH_SEVERE_THRESHOLD_PCT:
        base += SOH_SEVERE_DERATING_PCT
    elif bat.soh_pct < SOH_MODERATE_THRESHOLD_PCT:
        base += SOH_MODERATE_DERATING_PCT

    return base


def available_kwh(bat: BatteryInfo) -> float:
    """Calculate available energy above effective min_soc."""
    eff_min = effective_min_soc(bat)
    return max(0.0, (bat.soc - eff_min) / 100 * bat.cap_kwh)


def calculate_proportional_discharge(
    batteries: list[BatteryInfo],
    total_watts: int,
) -> BalancerResult:
    """Calculate proportional discharge across batteries.

    All batteries should reach min_soc at the same time.
    Batteries at or below min_soc get 0W.
    Cold batteries get higher min_soc (20% instead of 15%).

    Args:
        batteries: List of battery states.
        total_watts: Total discharge power requested (W).

    Returns:
        BalancerResult with per-battery allocations.
    """
    if not batteries or total_watts <= 0:
        return BalancerResult(
            allocations=[
                BatteryAllocation(
                    id=b.id,
                    watts=0,
                    share_pct=0,
                    _available_kwh=available_kwh(b),
                    effective_min_soc=effective_min_soc(b),
                    at_min_soc=b.soc <= effective_min_soc(b),
                )
                for b in batteries
            ],
            total_w=0,
            balanced=True,
        )

    # Calculate available energy per battery
    avail = [(b, available_kwh(b)) for b in batteries]
    total_avail = sum(a for _, a in avail)

    allocations = []
    actual_total = 0

    for bat, bat_avail in avail:
        eff_min = effective_min_soc(bat)
        at_min = bat.soc <= eff_min

        if at_min or total_avail <= 0 or bat_avail <= 0:
            allocations.append(
                BatteryAllocation(
                    id=bat.id,
                    watts=0,
                    share_pct=0,
                    _available_kwh=bat_avail,
                    effective_min_soc=eff_min,
                    at_min_soc=at_min,
                )
            )
            continue

        share = bat_avail / total_avail
        watts = int(total_watts * share)
        # Clamp to per-battery max
        watts = min(watts, int(bat.max_discharge_w))

        # EXP-07: Cold-lock discharge blocking (LFP safety)
        if bat.cell_temp_c < COLD_DISCHARGE_BLOCK_C:
            watts = 0
        elif bat.cell_temp_c < COLD_DISCHARGE_DERATING_C:
            watts = int(watts * 0.5)

        actual_total += watts

        allocations.append(
            BatteryAllocation(
                id=bat.id,
                watts=watts,
                share_pct=round(share * 100, 1),
                _available_kwh=bat_avail,
                effective_min_soc=eff_min,
                at_min_soc=False,
            )
        )

    # Check if balanced (all non-min batteries have similar time to min)
    active = [a for a in allocations if a.watts > 0 and a.available_kwh > 0]
    balanced = True
    if len(active) >= 2:
        times = [a.available_kwh / (a.watts / 1000) for a in active]
        if max(times) - min(times) > 0.5:  # More than 30 min difference
            balanced = False

    return BalancerResult(
        allocations=allocations,
        total_w=actual_total,
        balanced=balanced,
    )


def redistribute_on_depletion(
    batteries: list[BatteryInfo],
    total_watts: int,
) -> BalancerResult:
    """When one battery hits effective_min_soc, redistribute its share to others.

    If battery A is at effective_min_soc but battery B has capacity,
    battery B takes the full discharge load.
    If all batteries at effective_min_soc, return 0W for all.

    Uses a 1% margin per battery: soc <= effective_min_soc + 1 → excluded.
    Cold batteries (effective_min_soc=20%) use a higher threshold than warm ones.
    """
    margin = 1.0

    # Split into available vs depleted using per-battery effective_min_soc
    available = [b for b in batteries if b.soc > effective_min_soc(b) + margin]
    depleted = [b for b in batteries if b.soc <= effective_min_soc(b) + margin]

    # Build depleted allocations (0W each)
    depleted_allocs = [
        BatteryAllocation(
            id=b.id,
            watts=0,
            share_pct=0,
            _available_kwh=available_kwh(b),
            effective_min_soc=effective_min_soc(b),
            at_min_soc=True,
        )
        for b in depleted
    ]

    if not available or total_watts <= 0:
        # All depleted or nothing to discharge
        all_allocs = depleted_allocs + [
            BatteryAllocation(
                id=b.id,
                watts=0,
                share_pct=0,
                _available_kwh=available_kwh(b),
                effective_min_soc=effective_min_soc(b),
                at_min_soc=b.soc <= effective_min_soc(b),
            )
            for b in available
        ]
        return BalancerResult(allocations=all_allocs, total_w=0, balanced=True)

    # Redistribute proportionally among available batteries
    avail_kwh = [(b, available_kwh(b)) for b in available]
    total_avail = sum(a for _, a in avail_kwh)

    active_allocs = []
    actual_total = 0

    for bat, bat_avail in avail_kwh:
        eff_min = effective_min_soc(bat)
        if total_avail <= 0 or bat_avail <= 0:
            active_allocs.append(
                BatteryAllocation(
                    id=bat.id,
                    watts=0,
                    share_pct=0,
                    _available_kwh=bat_avail,
                    effective_min_soc=eff_min,
                    at_min_soc=False,
                )
            )
            continue

        share = bat_avail / total_avail
        watts = int(total_watts * share)
        watts = min(watts, int(bat.max_discharge_w))
        actual_total += watts

        active_allocs.append(
            BatteryAllocation(
                id=bat.id,
                watts=watts,
                share_pct=round(share * 100, 1),
                _available_kwh=bat_avail,
                effective_min_soc=eff_min,
                at_min_soc=False,
            )
        )

    return BalancerResult(
        allocations=depleted_allocs + active_allocs,
        total_w=actual_total,
        balanced=len(active_allocs) > 0,
    )


def calculate_proportional_charge(
    batteries: list[BatteryInfo],
    total_watts: int,
    max_soc: float = 100.0,
) -> BalancerResult:
    """Calculate proportional charge — fill emptiest first.

    Inverse of discharge: batteries with more room get more power.
    """
    if not batteries or total_watts <= 0:
        return BalancerResult(
            allocations=[
                BatteryAllocation(
                    id=b.id,
                    watts=0,
                    share_pct=0,
                    _available_kwh=0,
                    effective_min_soc=effective_min_soc(b),
                    at_min_soc=False,
                )
                for b in batteries
            ],
            total_w=0,
            balanced=True,
        )

    allocations = []
    actual_total = 0

    # Room = how much each battery can absorb
    rooms = []
    for bat in batteries:
        eff_min = effective_min_soc(bat)
        # Skip cold batteries (can't charge)
        if bat.cell_temp_c < bat.cold_temp_c:
            rooms.append((bat, 0.0))
            continue
        room = max(0.0, (max_soc - bat.soc) / 100 * bat.cap_kwh)
        rooms.append((bat, room))

    total_room = sum(r for _, r in rooms)

    for bat, room in rooms:
        eff_min = effective_min_soc(bat)

        if total_room <= 0 or room <= 0:
            allocations.append(
                BatteryAllocation(
                    id=bat.id,
                    watts=0,
                    share_pct=0,
                    _available_kwh=room,
                    effective_min_soc=eff_min,
                    at_min_soc=bat.soc <= eff_min,
                )
            )
            continue

        share = room / total_room
        watts = int(total_watts * share)
        watts = min(watts, int(bat.max_charge_w))
        actual_total += watts

        allocations.append(
            BatteryAllocation(
                id=bat.id,
                watts=watts,
                share_pct=round(share * 100, 1),
                _available_kwh=room,
                effective_min_soc=eff_min,
                at_min_soc=False,
            )
        )

    return BalancerResult(
        allocations=allocations,
        total_w=actual_total,
        balanced=True,
    )
