"""CARMA Box — Battery Health & Degradation Tracking (PLAT-963).

Pure Python. No HA imports. Fully testable.

Tracks battery roundtrip efficiency over time, cycle counting,
and temperature impact on performance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# EMA alpha for efficiency tracking
EFFICIENCY_EMA_ALPHA = 0.05  # Very slow — efficiency changes gradually
MIN_CYCLE_SAMPLES = 10


@dataclass
class CycleRecord:
    """One charge/discharge cycle."""

    date: str  # ISO date
    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0
    efficiency: float = 0.0  # discharge/charge ratio
    avg_temp_c: float | None = None


@dataclass
class BatteryHealthState:
    """Battery health and degradation tracking state."""

    # Current cycle accumulator
    current_charge_kwh: float = 0.0
    current_discharge_kwh: float = 0.0
    current_temp_sum: float = 0.0
    current_temp_count: int = 0

    # Learned roundtrip efficiency (EMA)
    roundtrip_efficiency: float = 0.90  # Default 90%

    # Cycle counting
    total_cycles: float = 0.0  # Full equivalent cycles
    total_charge_kwh: float = 0.0
    total_discharge_kwh: float = 0.0

    # Monthly efficiency history (for trend)
    monthly_efficiency: list[dict[str, Any]] = field(default_factory=list)

    # Temperature impact: [temp_bin] → efficiency
    # Bins: <0°C, 0-10, 10-20, 20-30, 30+
    temp_efficiency: list[float] = field(default_factory=lambda: [0.82, 0.87, 0.90, 0.90, 0.88])
    temp_efficiency_counts: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])

    # Daily cycle records (last 90 days)
    daily_records: list[CycleRecord] = field(default_factory=list)

    # Capacity estimate (% of nominal)
    estimated_capacity_pct: float = 100.0


def _temp_bin(temp_c: float) -> int:
    """Convert temperature to bin (0-4)."""
    if temp_c < 0:
        return 0
    if temp_c < 10:
        return 1
    if temp_c < 20:
        return 2
    if temp_c < 30:
        return 3
    return 4


def record_charge(state: BatteryHealthState, kwh: float, temp_c: float | None = None) -> None:
    """Record energy charged into battery.

    Args:
        state: Current health state.
        kwh: Energy charged (kWh, positive).
        temp_c: Battery/ambient temperature (°C).
    """
    if kwh <= 0:
        return
    state.current_charge_kwh += kwh
    state.total_charge_kwh += kwh
    if temp_c is not None:
        state.current_temp_sum += temp_c
        state.current_temp_count += 1


def record_discharge(state: BatteryHealthState, kwh: float, temp_c: float | None = None) -> None:
    """Record energy discharged from battery.

    Args:
        state: Current health state.
        kwh: Energy discharged (kWh, positive).
        temp_c: Battery/ambient temperature (°C).
    """
    if kwh <= 0:
        return
    state.current_discharge_kwh += kwh
    state.total_discharge_kwh += kwh
    if temp_c is not None:
        state.current_temp_sum += temp_c
        state.current_temp_count += 1


def complete_cycle(
    state: BatteryHealthState,
    date_str: str,
    battery_cap_kwh: float = 20.0,
) -> CycleRecord | None:
    """Complete a daily cycle and update efficiency tracking.

    Call once per day (e.g., at midnight).

    Args:
        state: Current health state.
        date_str: ISO date string.
        battery_cap_kwh: Nominal battery capacity.

    Returns:
        CycleRecord if meaningful data, None otherwise.
    """
    charge = state.current_charge_kwh
    discharge = state.current_discharge_kwh

    if charge < 0.5 and discharge < 0.5:
        # No meaningful activity
        state.current_charge_kwh = 0
        state.current_discharge_kwh = 0
        state.current_temp_sum = 0
        state.current_temp_count = 0
        return None

    # Calculate efficiency
    efficiency = discharge / charge if charge > 0.5 else state.roundtrip_efficiency
    efficiency = max(0.5, min(1.0, efficiency))  # Clamp to reasonable range

    # Update EMA efficiency
    old = state.roundtrip_efficiency
    state.roundtrip_efficiency = (
        EFFICIENCY_EMA_ALPHA * efficiency + (1 - EFFICIENCY_EMA_ALPHA) * old
    )

    # Update cycle count (full equivalent cycles)
    max_energy = max(charge, discharge)
    if battery_cap_kwh > 0:
        state.total_cycles += max_energy / battery_cap_kwh

    # Temperature tracking
    avg_temp = None
    if state.current_temp_count > 0:
        avg_temp = state.current_temp_sum / state.current_temp_count
        t_bin = _temp_bin(avg_temp)
        old_te = state.temp_efficiency[t_bin]
        state.temp_efficiency[t_bin] = (
            EFFICIENCY_EMA_ALPHA * efficiency + (1 - EFFICIENCY_EMA_ALPHA) * old_te
        )
        state.temp_efficiency_counts[t_bin] += 1

    record = CycleRecord(
        date=date_str,
        charge_kwh=round(charge, 2),
        discharge_kwh=round(discharge, 2),
        efficiency=round(efficiency, 4),
        avg_temp_c=round(avg_temp, 1) if avg_temp is not None else None,
    )

    # Store daily record
    state.daily_records.append(record)
    if len(state.daily_records) > 90:
        state.daily_records = state.daily_records[-90:]

    # Reset accumulators
    state.current_charge_kwh = 0
    state.current_discharge_kwh = 0
    state.current_temp_sum = 0
    state.current_temp_count = 0

    return record


def record_monthly_snapshot(
    state: BatteryHealthState,
    month: int,
    year: int,
) -> None:
    """Snapshot monthly efficiency for long-term trend tracking.

    Call once per month.
    """
    # Check if already recorded this month
    for rec in state.monthly_efficiency:
        if rec.get("month") == month and rec.get("year") == year:
            return

    state.monthly_efficiency.append(
        {
            "month": month,
            "year": year,
            "efficiency": round(state.roundtrip_efficiency, 4),
            "total_cycles": round(state.total_cycles, 1),
            "estimated_capacity_pct": round(state.estimated_capacity_pct, 1),
        }
    )

    # Keep last 24 months
    if len(state.monthly_efficiency) > 24:
        state.monthly_efficiency = state.monthly_efficiency[-24:]


def estimate_degradation(state: BatteryHealthState) -> float:
    """Estimate battery capacity degradation based on cycle count.

    Uses a simple linear degradation model:
    - LiFePO4 batteries: ~80% capacity after 6000 cycles
    - ~0.0033% per cycle

    Returns estimated remaining capacity as percentage (0-100).
    """
    # LiFePO4 degradation rate per cycle
    degradation_per_cycle = 0.0033  # 0.0033% per cycle
    degradation = state.total_cycles * degradation_per_cycle
    state.estimated_capacity_pct = round(max(60, 100 - degradation), 1)
    return state.estimated_capacity_pct


def efficiency_for_temperature(state: BatteryHealthState, temp_c: float) -> float:
    """Get expected roundtrip efficiency at a given temperature.

    Returns learned efficiency or default if insufficient data.
    """
    t_bin = _temp_bin(temp_c)
    if state.temp_efficiency_counts[t_bin] >= MIN_CYCLE_SAMPLES:
        return round(state.temp_efficiency[t_bin], 3)
    return state.roundtrip_efficiency


def efficiency_trend(state: BatteryHealthState) -> str:
    """Is efficiency improving, stable, or degrading?

    Based on monthly snapshots.
    """
    if len(state.monthly_efficiency) < 3:
        return "insufficient_data"

    recent = [r["efficiency"] for r in state.monthly_efficiency[-3:]]
    older = (
        [r["efficiency"] for r in state.monthly_efficiency[-6:-3]]
        if len(state.monthly_efficiency) >= 6
        else []
    )

    if not older:
        return "insufficient_data"

    recent_avg = sum(recent) / len(recent)
    older_avg = sum(older) / len(older)

    if recent_avg < older_avg * 0.98:
        return "degrading"
    if recent_avg > older_avg * 1.02:
        return "improving"
    return "stable"


def health_summary(state: BatteryHealthState) -> dict[str, Any]:
    """Full health summary for dashboard/diagnostics."""
    return {
        "roundtrip_efficiency_pct": round(state.roundtrip_efficiency * 100, 1),
        "total_cycles": round(state.total_cycles, 1),
        "estimated_capacity_pct": round(state.estimated_capacity_pct, 1),
        "efficiency_trend": efficiency_trend(state),
        "total_charge_kwh": round(state.total_charge_kwh, 0),
        "total_discharge_kwh": round(state.total_discharge_kwh, 0),
        "temp_efficiency": {
            "<0°C": round(state.temp_efficiency[0] * 100, 1),
            "0-10°C": round(state.temp_efficiency[1] * 100, 1),
            "10-20°C": round(state.temp_efficiency[2] * 100, 1),
            "20-30°C": round(state.temp_efficiency[3] * 100, 1),
            ">30°C": round(state.temp_efficiency[4] * 100, 1),
        },
    }


def state_to_dict(state: BatteryHealthState) -> dict[str, Any]:
    """Serialize for persistent storage."""
    return {
        "roundtrip_efficiency": state.roundtrip_efficiency,
        "total_cycles": state.total_cycles,
        "total_charge_kwh": state.total_charge_kwh,
        "total_discharge_kwh": state.total_discharge_kwh,
        "estimated_capacity_pct": state.estimated_capacity_pct,
        "temp_efficiency": state.temp_efficiency,
        "temp_efficiency_counts": state.temp_efficiency_counts,
        "monthly_efficiency": state.monthly_efficiency[-24:],
        "daily_records": [
            {
                "date": r.date,
                "charge_kwh": r.charge_kwh,
                "discharge_kwh": r.discharge_kwh,
                "efficiency": r.efficiency,
                "avg_temp_c": r.avg_temp_c,
            }
            for r in state.daily_records[-90:]
        ],
        "current_charge_kwh": state.current_charge_kwh,
        "current_discharge_kwh": state.current_discharge_kwh,
    }


def state_from_dict(data: dict[str, Any]) -> BatteryHealthState:
    """Deserialize from storage."""
    if not data or not isinstance(data, dict):
        return BatteryHealthState()
    try:
        daily = [
            CycleRecord(
                date=str(d["date"]),
                charge_kwh=float(d.get("charge_kwh", 0)),
                discharge_kwh=float(d.get("discharge_kwh", 0)),
                efficiency=float(d.get("efficiency", 0.9)),
                avg_temp_c=float(d["avg_temp_c"]) if d.get("avg_temp_c") is not None else None,
            )
            for d in data.get("daily_records", [])
            if isinstance(d, dict)
        ]
        te = data.get("temp_efficiency", [0.82, 0.87, 0.90, 0.90, 0.88])
        tec = data.get("temp_efficiency_counts", [0, 0, 0, 0, 0])
        return BatteryHealthState(
            roundtrip_efficiency=float(data.get("roundtrip_efficiency", 0.90)),
            total_cycles=float(data.get("total_cycles", 0)),
            total_charge_kwh=float(data.get("total_charge_kwh", 0)),
            total_discharge_kwh=float(data.get("total_discharge_kwh", 0)),
            estimated_capacity_pct=float(data.get("estimated_capacity_pct", 100)),
            temp_efficiency=[float(v) for v in te[:5]],
            temp_efficiency_counts=[int(v) for v in tec[:5]],
            monthly_efficiency=list(data.get("monthly_efficiency", [])),
            daily_records=daily,
            current_charge_kwh=float(data.get("current_charge_kwh", 0)),
            current_discharge_kwh=float(data.get("current_discharge_kwh", 0)),
        )
    except (KeyError, ValueError, TypeError):
        return BatteryHealthState()
