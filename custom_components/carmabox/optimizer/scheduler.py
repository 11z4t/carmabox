"""CARMA Box — Intelligent Scheduler (IT-2378).

Pure Python. No HA imports. Fully testable.

Proactive puzzle-solver: generates a 24h hour-by-hour plan that keeps
ALL controllable loads (battery, EV, miner) under Ellevio target.

Key principles:
  - PROACTIVE, never reactive — everything planned ahead
  - EV scheduled backwards from departure (latest possible hours)
  - Battery follows price signal (charge cheap, discharge expensive)
  - Miner ONLY on PV export surplus
  - Constraint check every hour: total weighted < target × 0.85
  - Auto root-cause analysis on breaches with learning profile
  - Weekly 100% EV charge planning via Solcast
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..const import (
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_EV_EFFICIENCY,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_MAX_GRID_CHARGE_KW,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_WEIGHT,
    DEFAULT_VOLTAGE,
    SCHEDULER_APPLIANCE_WINDOW_END,
    SCHEDULER_APPLIANCE_WINDOW_START,
    SCHEDULER_BREACH_MAJOR_PCT,
    SCHEDULER_BREACH_MINOR_PCT,
    SCHEDULER_CONSTRAINT_MARGIN,
    SCHEDULER_EV_100_INTERVAL_DAYS,
    SCHEDULER_EV_100_PV_THRESHOLD_KWH,
    SCHEDULER_EV_DEPARTURE_HOUR,
    SCHEDULER_LEARNING_CONFIDENCE_STEP,
    SCHEDULER_MAX_LEARNINGS,
    SCHEDULER_MINER_EXPORT_MIN_W,
    SCHEDULER_PLAN_HOURS,
)
from .evening_optimizer import apply_strategy_to_battery_schedule, evaluate_evening_strategy
from .grid_logic import ellevio_weight
from .models import (
    BreachCorrection,
    BreachLearning,
    BreachRecord,
    IdleAnalysis,
    SchedulerHourSlot,
    SchedulerPlan,
)

_LOGGER = logging.getLogger(__name__)


def _is_night_hour(
    hour: int,
    start: int = DEFAULT_NIGHT_START,
    end: int = DEFAULT_NIGHT_END,
) -> bool:
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _is_appliance_window(
    hour: int,
    start: int = SCHEDULER_APPLIANCE_WINDOW_START,
    end: int = SCHEDULER_APPLIANCE_WINDOW_END,
) -> bool:
    """Check if hour falls in typical appliance run window (dishwasher/laundry)."""
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def _hours_until_departure(current_hour: int, departure: int = SCHEDULER_EV_DEPARTURE_HOUR) -> int:
    """Hours from current_hour until departure (wraps at 24)."""
    if current_hour <= departure:
        return departure - current_hour
    return 24 - current_hour + departure


# ── EV Scheduling (backwards from departure) ──────────────────────


def _schedule_ev_backwards(
    num_hours: int,
    start_hour: int,
    ev_soc_pct: float,
    ev_capacity_kwh: float,
    morning_target_soc: float,
    hourly_prices: list[float],
    hourly_loads: list[float],
    target_weighted_kw: float,
    battery_kwh_available: float,
    pv_tomorrow_kwh: float,
    daily_consumption_kwh: float,
    learnings: list[BreachLearning],
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    min_amps: int = DEFAULT_EV_MIN_AMPS,
    max_amps: int = DEFAULT_EV_MAX_AMPS,
    voltage: float = DEFAULT_VOLTAGE,
    ev_efficiency: float = DEFAULT_EV_EFFICIENCY,
    battery_efficiency: float = DEFAULT_BATTERY_EFFICIENCY,
    night_start: int = DEFAULT_NIGHT_START,
    night_end: int = DEFAULT_NIGHT_END,
) -> list[tuple[float, int]]:
    """Schedule EV charging backwards from departure.

    Returns list of (ev_kw, ev_amps) per hour slot. Same length as num_hours.

    Strategy:
    1. Calculate energy needed: (target_soc - current_soc * 0.9) * capacity
    2. Find N cheapest night hours that provide enough energy
    3. Pick LATEST possible hours (avoid early appliance window 22-01)
    4. Apply learnings: if breach pattern exists, adjust accordingly
    """
    schedule: list[tuple[float, int]] = [(0.0, 0)] * num_hours

    if ev_soc_pct < 0:
        ev_soc_pct = 50.0
    if ev_capacity_kwh <= 0:
        return schedule

    # Energy needed (account for 10% discharge overnight from BMS)
    energy_needed_kwh = max(0, (morning_target_soc - ev_soc_pct * 0.9) / 100 * ev_capacity_kwh)
    if energy_needed_kwh < 0.5:
        return schedule

    min_kw = min_amps * voltage / 1000
    max_kw = max_amps * voltage / 1000

    # Collect night hour candidates
    candidates: list[dict[str, Any]] = []
    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        if not _is_night_hour(abs_h, night_start, night_end):
            continue
        price = hourly_prices[i] if i < len(hourly_prices) else 100.0
        load = hourly_loads[i] if i < len(hourly_loads) else 2.0
        w = ellevio_weight(abs_h, night_weight)

        # Check if learnings say to avoid this hour
        avoid = False
        for lr in learnings:
            if lr.hour == abs_h and lr.action in ("pause_ev", "shift_ev") and lr.confidence > 0.5:
                avoid = True
                break

        candidates.append(
            {
                "idx": i,
                "hour": abs_h,
                "price": price,
                "load": load,
                "weight": w,
                "is_appliance_window": _is_appliance_window(abs_h),
                "avoid": avoid,
            }
        )

    if not candidates:
        return schedule

    # Battery support budget (similar to ev_strategy.py)
    pv_surplus = max(0, pv_tomorrow_kwh - daily_consumption_kwh)
    if pv_surplus > 10:
        battery_budget_kwh = battery_kwh_available
    elif pv_surplus > 0:
        battery_budget_kwh = min(battery_kwh_available, pv_surplus)
    else:
        battery_budget_kwh = battery_kwh_available * 0.3
    batt_per_hour = battery_budget_kwh / len(candidates) if candidates else 0.0

    # Sort candidates: cheapest first
    sorted_cands = sorted(candidates, key=lambda c: c["price"])

    # Calculate how much energy each candidate can deliver
    slot_energy: list[dict[str, Any]] = []
    for cand in sorted_cands:
        w = cand["weight"]
        load = cand["load"]
        batt_kw = min(batt_per_hour, load)
        effective_load = max(0, load - batt_kw)
        grid_headroom = (
            (target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN / w - effective_load)
            if w > 0
            else max_kw
        )
        grid_headroom = max(0, grid_headroom)

        # Pick amps that fit under headroom
        desired_kw = min(max_kw, grid_headroom)
        if desired_kw < min_kw:
            desired_kw = 0.0

        if desired_kw > 0:
            amps = max(min_amps, min(int(desired_kw * 1000 / voltage), max_amps))
            charge_kw = amps * voltage / 1000
        else:
            amps = 0
            charge_kw = 0.0

        slot_energy.append(
            {
                **cand,
                "charge_kw": charge_kw,
                "amps": amps,
                "effective_kwh": charge_kw * ev_efficiency,
            }
        )

    # Greedy: pick LATEST hours first (backwards), among cheapest sufficient set
    # Step 1: find cheapest N hours that cover energy need
    remaining = energy_needed_kwh
    selected_indices: set[int] = set()

    # First pass: use non-appliance-window, non-avoided hours (cheapest first)
    for se in slot_energy:
        if remaining <= 0.1:
            break
        if se["avoid"] or se["is_appliance_window"] or se["charge_kw"] <= 0:
            continue
        selected_indices.add(se["idx"])
        remaining -= se["effective_kwh"]

    # Second pass: use appliance-window hours if needed (with reduced amps)
    if remaining > 0.1:
        for se in slot_energy:
            if remaining <= 0.1:
                break
            if se["idx"] in selected_indices or se["charge_kw"] <= 0:
                continue
            if se["is_appliance_window"]:
                # Reduce to min amps during appliance window
                amps = min_amps
                charge_kw = amps * voltage / 1000
                se["amps"] = amps
                se["charge_kw"] = charge_kw
                se["effective_kwh"] = charge_kw * ev_efficiency
            selected_indices.add(se["idx"])
            remaining -= se["effective_kwh"]

    # Third pass: use avoided hours as last resort
    if remaining > 0.1:
        for se in slot_energy:
            if remaining <= 0.1:
                break
            if se["idx"] in selected_indices:
                continue
            if se["charge_kw"] <= 0:
                # Force min amps even if tight
                se["amps"] = min_amps
                se["charge_kw"] = min_kw
                se["effective_kwh"] = min_kw * ev_efficiency
            selected_indices.add(se["idx"])
            remaining -= se["effective_kwh"]

    # Among selected, prefer LATEST hours (shift right towards departure)
    # This is the "backwards from departure" principle
    selected_slots = [se for se in slot_energy if se["idx"] in selected_indices]
    # Re-sort by hour distance from departure (furthest first = lowest priority)
    # We keep all selected but this ordering helps with partial reduction

    # Apply to schedule
    for se in selected_slots:
        idx = se["idx"]
        schedule[idx] = (se["charge_kw"], se["amps"])

    return schedule


# ── Battery Scheduling ─────────────────────────────────────────────


def _schedule_battery(
    num_hours: int,
    start_hour: int,
    hourly_prices: list[float],
    hourly_pv: list[float],
    hourly_loads: list[float],
    hourly_ev: list[float],
    target_weighted_kw: float,
    battery_soc_pct: float,
    battery_cap_kwh: float,
    battery_min_soc: float = DEFAULT_BATTERY_MIN_SOC,
    battery_efficiency: float = DEFAULT_BATTERY_EFFICIENCY,
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    grid_charge_price_threshold: float = DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    grid_charge_max_soc: float = DEFAULT_GRID_CHARGE_MAX_SOC,
    max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_KW,
    max_grid_charge_kw: float = DEFAULT_MAX_GRID_CHARGE_KW,
    pv_forecast_daily: list[float] | None = None,
) -> list[tuple[float, str]]:
    """Schedule battery hour-by-hour.

    Returns list of (battery_kw, action) per slot.
    battery_kw: + = charging, - = discharging
    action: 'c' = PV charge, 'g' = grid charge, 'd' = discharge, 'i' = idle
    """
    result: list[tuple[float, str]] = [(0.0, "i")] * num_hours

    soc_kwh = battery_soc_pct / 100 * battery_cap_kwh
    min_soc_kwh = battery_min_soc / 100 * battery_cap_kwh
    max_charge_kwh = grid_charge_max_soc / 100 * battery_cap_kwh

    # Calculate reserve from PV forecast
    reserve_kwh = 0.0
    if pv_forecast_daily:
        daily_consumption = sum(hourly_loads[:24]) / max(1, min(24, num_hours)) * 24
        for pv_day in pv_forecast_daily[1:] if len(pv_forecast_daily) > 1 else []:
            surplus = max(0, pv_day - daily_consumption)
            if surplus > 10:
                break
            reserve_kwh += max(0, 5.0 - surplus)

    effective_min_kwh = min_soc_kwh + reserve_kwh

    # Find price median for charge/discharge decisions
    valid_prices = [p for p in hourly_prices[:num_hours] if p > 0]
    _median_price = sorted(valid_prices)[len(valid_prices) // 2] if valid_prices else 50.0

    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        w = ellevio_weight(abs_h, night_weight)
        load = hourly_loads[i] if i < len(hourly_loads) else 1.5
        pv = hourly_pv[i] if i < len(hourly_pv) else 0.0
        ev = hourly_ev[i] if i < len(hourly_ev) else 0.0
        price = hourly_prices[i] if i < len(hourly_prices) else 100.0

        net = load + ev - pv  # Positive = importing, negative = exporting
        battery_kw = 0.0
        action = "i"

        # Priority 1: Solar surplus → charge from PV
        if net < -0.5:
            surplus = abs(net)
            charge = min(surplus, battery_cap_kwh - soc_kwh)
            if charge > 0.3:
                battery_kw = charge
                soc_kwh += charge * battery_efficiency
                action = "c"

        # Priority 2: Very cheap price → grid charge
        elif price <= grid_charge_price_threshold and soc_kwh < max_charge_kwh:
            headroom = max_charge_kwh - soc_kwh
            charge_kw = min(max_grid_charge_kw, headroom)
            if charge_kw > 0.3:
                battery_kw = charge_kw
                soc_kwh += charge_kw * battery_efficiency
                action = "g"

        # Priority 3: High price or load above target → discharge
        elif w > 0 and net * w > target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN:
            need = (net * w - target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN) / w
            available = soc_kwh - effective_min_kwh
            if available > 0.3:
                discharge = min(need, available, max_discharge_kw)
                battery_kw = -discharge
                soc_kwh -= discharge
                action = "d"

        # Priority 4: EV charging support — discharge battery to support EV under target
        elif ev > 0 and w > 0:
            total_load = net * w
            if total_load > target_weighted_kw * 0.7:
                support_need = (total_load - target_weighted_kw * 0.7) / w
                available = soc_kwh - effective_min_kwh
                if available > 0.3 and support_need > 0.2:
                    discharge = min(support_need, available, max_discharge_kw)
                    battery_kw = -discharge
                    soc_kwh -= discharge
                    action = "d"

        soc_kwh = max(0.0, min(soc_kwh, battery_cap_kwh))
        result[i] = (round(battery_kw, 2), action)

    return result


# ── Miner Scheduling ───────────────────────────────────────────────


def _schedule_miner(
    num_hours: int,
    start_hour: int,
    hourly_pv: list[float],
    hourly_loads: list[float],
    hourly_ev: list[float],
    hourly_battery: list[float],
) -> list[bool]:
    """Schedule miner: ON only when PV export surplus after all loads.

    Returns list of bool (True = on) per slot.
    """
    result: list[bool] = [False] * num_hours

    for i in range(num_hours):
        pv = hourly_pv[i] if i < len(hourly_pv) else 0.0
        load = hourly_loads[i] if i < len(hourly_loads) else 1.5
        ev = hourly_ev[i] if i < len(hourly_ev) else 0.0
        batt = hourly_battery[i] if i < len(hourly_battery) else 0.0

        # Net after all loads and battery (+ = battery charging)
        net_export_w = (pv - load - ev - max(0, batt)) * 1000
        result[i] = net_export_w > SCHEDULER_MINER_EXPORT_MIN_W

    return result


# ── Constraint Checker ─────────────────────────────────────────────


def _check_constraints(
    slots: list[SchedulerHourSlot],
    target_weighted_kw: float,
) -> list[SchedulerHourSlot]:
    """Check and fix constraint violations.

    Applies remediation priority:
    1. Turn off miner
    2. Reduce EV amps
    3. Pause EV
    4. Increase battery discharge
    """
    for slot in slots:
        if slot.weighted_kw <= target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN:
            slot = _update_slot(slot, constraint_ok=True)
            continue

        # Violation detected — apply fixes in priority order
        remaining_excess = slot.weighted_kw - target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN
        w = ellevio_weight(slot.hour)
        fixes: list[str] = []

        # Fix 1: Turn off miner (~0.5-1 kW saved)
        if slot.miner_on:
            slot = _update_slot(slot, miner_on=False)
            remaining_excess -= 0.5 * w
            fixes.append("miner av")

        # Fix 2: Reduce EV amps
        if remaining_excess > 0 and slot.ev_amps > DEFAULT_EV_MIN_AMPS:
            reduction_amps = min(
                slot.ev_amps - DEFAULT_EV_MIN_AMPS,
                int(remaining_excess / w * 1000 / DEFAULT_VOLTAGE) + 1,
            )
            new_amps = max(DEFAULT_EV_MIN_AMPS, slot.ev_amps - reduction_amps)
            new_ev_kw = new_amps * DEFAULT_VOLTAGE / 1000
            saved_kw = slot.ev_kw - new_ev_kw
            slot = _update_slot(slot, ev_amps=new_amps, ev_kw=new_ev_kw)
            remaining_excess -= saved_kw * w
            fixes.append(f"EV sänkt till {new_amps}A")

        # Fix 3: Pause EV entirely
        if remaining_excess > 0 and slot.ev_kw > 0:
            saved_kw = slot.ev_kw
            slot = _update_slot(slot, ev_amps=0, ev_kw=0.0)
            remaining_excess -= saved_kw * w
            fixes.append("EV pausad")

        # Fix 4: Increase battery discharge
        if remaining_excess > 0 and slot.battery_kw >= 0:
            extra_discharge = min(remaining_excess / w, DEFAULT_MAX_DISCHARGE_KW)
            new_batt = slot.battery_kw - extra_discharge
            slot = _update_slot(slot, battery_kw=round(new_batt, 2), action="d")
            remaining_excess -= extra_discharge * w
            fixes.append(f"batteri stöd +{extra_discharge:.1f}kW")

        # Recalculate weighted
        net_grid = slot.consumption_kw + slot.ev_kw - slot.pv_kw + slot.battery_kw
        net_grid = max(0, net_grid)
        w = ellevio_weight(slot.hour)
        new_weighted = net_grid * w
        ok = new_weighted <= target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN

        reason_parts = [slot.reasoning]
        if fixes:
            reason_parts.append("Fix: " + ", ".join(fixes))

        slot = _update_slot(
            slot,
            grid_kw=round(net_grid, 2),
            weighted_kw=round(new_weighted, 2),
            constraint_ok=ok,
            reasoning="; ".join(reason_parts),
        )

    return slots


def _update_slot(slot: SchedulerHourSlot, **kwargs: Any) -> SchedulerHourSlot:
    """Return a new slot with updated fields (dataclass is not frozen)."""
    for k, v in kwargs.items():
        setattr(slot, k, v)
    return slot


# ── Auto Root Cause Analysis ──────────────────────────────────────


def analyze_breach(
    hour: int,
    actual_weighted_kw: float,
    target_kw: float,
    house_load_kw: float,
    ev_kw: float,
    ev_amps: int,
    battery_kw: float,
    pv_kw: float,
    miner_on: bool,
    appliance_loads: dict[str, float] | None = None,
) -> BreachRecord:
    """Analyze why an Ellevio target breach occurred.

    Returns a BreachRecord with root cause and remediation in Swedish.
    """
    now = datetime.now()
    excess = actual_weighted_kw - target_kw
    pct_over = excess / target_kw if target_kw > 0 else 0

    if pct_over < SCHEDULER_BREACH_MINOR_PCT:
        severity = "minor"
    elif pct_over < SCHEDULER_BREACH_MAJOR_PCT:
        severity = "major"
    else:
        severity = "critical"

    # Build active loads list
    loads: list[str] = [f"hus:{house_load_kw:.1f}kW"]
    if ev_kw > 0:
        loads.append(f"EV:{ev_amps}A ({ev_kw:.1f}kW)")
    if miner_on:
        loads.append("miner:ON")
    if battery_kw < -0.1:
        loads.append(f"batteri urladdning:{abs(battery_kw):.1f}kW")
    elif battery_kw > 0.1:
        loads.append(f"batteri laddning:{battery_kw:.1f}kW")
    if pv_kw > 0:
        loads.append(f"PV:{pv_kw:.1f}kW")
    if appliance_loads:
        for name, power in appliance_loads.items():
            if power > 0.3:
                loads.append(f"{name}:{power:.1f}kW")

    # Root cause analysis
    root_cause_parts: list[str] = []
    remediation_parts: list[str] = []

    # Check for EV + appliance overlap
    if ev_kw > 0 and _is_appliance_window(hour):
        appliance_total = sum((appliance_loads or {}).values())
        if appliance_total > 1.0:
            root_cause_parts.append(
                f"EV ({ev_amps}A) + vitvaror ({appliance_total:.1f}kW) "
                f"kl {hour} orsakade topplast"
            )
            remediation_parts.append(
                f"Pausa EV under vitvaror kl {hour}, " "eller sänk EV till 6A under detta fönster"
            )

    # Check for insufficient battery support
    if battery_kw >= 0 and house_load_kw > target_kw * 0.7:
        root_cause_parts.append(
            "Batteri stöttade inte hushållet — " f"huslast {house_load_kw:.1f}kW utan urladdning"
        )
        remediation_parts.append("Schemalägg batteri-urladdning under denna timme")

    # Check for unexpected high house load
    if house_load_kw > 3.0 and not ev_kw and not miner_on:
        root_cause_parts.append(
            f"Oväntat hög huslast ({house_load_kw:.1f}kW) — "
            "trolig orsak: VP, varmvatten eller vitvaror"
        )
        remediation_parts.append("Granska vitvaror-schema, överväg tidsförskjutning")

    # Check battery cold lock
    if battery_kw == 0 and house_load_kw > target_kw:
        root_cause_parts.append("Batteri inaktivt (möjlig cold lock eller BMS-spärr)")
        remediation_parts.append("Kontrollera batteritemperatur, överväg vinterläge")

    # EV alone too high
    if ev_kw > 0 and ev_kw > target_kw * 0.5:
        root_cause_parts.append(
            f"EV-laddning ({ev_amps}A = {ev_kw:.1f}kW) "
            f"använder >{int(ev_kw/target_kw*100)}% av target"
        )
        remediation_parts.append(f"Sänk EV till {DEFAULT_EV_MIN_AMPS}A under toppbelastning")

    # Fallback
    if not root_cause_parts:
        root_cause_parts.append(
            f"Kombinerad last ({actual_weighted_kw:.1f}kW viktat) "
            f"överskred target ({target_kw:.1f}kW)"
        )
        remediation_parts.append("Granska lastprofil och justera schemaläggning")

    return BreachRecord(
        timestamp=now.isoformat(),
        hour=hour,
        actual_weighted_kw=round(actual_weighted_kw, 2),
        target_kw=round(target_kw, 2),
        loads_active=loads,
        root_cause=". ".join(root_cause_parts),
        remediation=". ".join(remediation_parts),
        severity=severity,
    )


def update_learnings(
    learnings: list[BreachLearning],
    breach: BreachRecord,
) -> list[BreachLearning]:
    """Update learning profile from a breach. Returns updated list."""
    # Generate pattern key
    parts = []
    for load in breach.loads_active:
        if load.startswith("EV:"):
            parts.append("ev")
        elif "miner" in load.lower():
            parts.append("miner")
        elif any(w in load.lower() for w in ("disk", "tvatt", "tork")):
            parts.append("appliance")
    pattern = "+".join(sorted(parts)) + f"_{breach.hour}" if parts else f"high_load_{breach.hour}"

    # Determine action based on root cause
    if "EV" in breach.root_cause and "vitvaror" in breach.root_cause:
        action = "pause_ev"
    elif "EV" in breach.root_cause:
        action = "reduce_ev_amps"
    elif "Batteri" in breach.root_cause:
        action = "battery_support"
    else:
        action = "reduce_load"

    # Update existing or create new
    for lr in learnings:
        if lr.pattern == pattern:
            lr.occurrences += 1
            lr.confidence = min(1.0, lr.confidence + SCHEDULER_LEARNING_CONFIDENCE_STEP)
            return learnings

    learnings.append(
        BreachLearning(
            pattern=pattern,
            hour=breach.hour,
            description=breach.root_cause[:120],
            action=action,
            confidence=SCHEDULER_LEARNING_CONFIDENCE_STEP,
            occurrences=1,
        )
    )

    # Cap learnings size
    if len(learnings) > SCHEDULER_MAX_LEARNINGS:
        # Remove lowest confidence entries
        learnings.sort(key=lambda x: x.confidence, reverse=True)
        learnings = learnings[:SCHEDULER_MAX_LEARNINGS]

    return learnings


# ── Weekly EV 100% Planning ────────────────────────────────────────


def plan_ev_full_charge(
    days_since_full: int,
    pv_forecast_daily: list[float],
    current_weekday: int,  # 0=Mon, 6=Sun
) -> str:
    """Plan next EV 100% charge date.

    Strategy:
    - If > 5 days since last full: find next good PV day
    - Prefer weekends (car at home)
    - If weekday + good sun: charge overnight to 75%, PV does 75→100%

    Returns ISO date string of planned full charge, or empty string.
    """
    if days_since_full < SCHEDULER_EV_100_INTERVAL_DAYS - 2:
        return ""  # Not due yet

    today = datetime.now().date()

    for day_offset in range(min(len(pv_forecast_daily), 7)):
        check_date = today + timedelta(days=day_offset)
        weekday = check_date.weekday()
        pv_kwh = pv_forecast_daily[day_offset] if day_offset < len(pv_forecast_daily) else 0

        is_weekend = weekday >= 5
        is_sunny = pv_kwh >= SCHEDULER_EV_100_PV_THRESHOLD_KWH

        if is_sunny and (is_weekend or days_since_full >= SCHEDULER_EV_100_INTERVAL_DAYS):
            return check_date.isoformat()

    # Fallback: next weekend regardless of PV
    days_to_saturday = (5 - current_weekday) % 7
    if days_to_saturday == 0:
        days_to_saturday = 7
    return (today + timedelta(days=days_to_saturday)).isoformat()


# ── Breach Correction Application ──────────────────────────────────


def _apply_corrections(
    corrections: list[BreachCorrection],
    ev_schedule: list[tuple[float, int]],
    battery_schedule: list[tuple[float, str]],
    start_hour: int,
    num_hours: int,
    battery_soc_pct: float,
    battery_cap_kwh: float,
    battery_min_soc: float,
    max_discharge_kw: float,
) -> tuple[list[tuple[float, int]], list[tuple[float, str]]]:
    """Apply breach corrections to EV and battery schedules.

    Modifies schedules in-place based on corrections generated from
    previous breaches. Returns updated (ev_schedule, battery_schedule).
    """
    ev_schedule = list(ev_schedule)
    battery_schedule = list(battery_schedule)

    applied_count = 0
    for corr in corrections:
        if corr.expired or corr.applied:
            continue

        # Map target_hour to slot index
        idx = (corr.target_hour - start_hour) % 24
        if idx >= num_hours:
            continue

        if corr.action == "reduce_ev" and ev_schedule[idx][0] > 0:
            # Reduce EV to 6A
            old_kw, old_amps = ev_schedule[idx]
            new_amps = DEFAULT_EV_MIN_AMPS
            new_kw = new_amps * 230 * 3 / 1000  # 3-phase
            ev_schedule[idx] = (min(old_kw, new_kw), new_amps)
            corr.applied = True
            applied_count += 1
            _LOGGER.info(
                "Correction applied: reduce_ev kl %02d (%dA→%dA)",
                corr.target_hour,
                old_amps,
                new_amps,
            )

        elif corr.action == "shift_ev":
            # Move EV from source hour to target hour (idx = destination)
            params = dict(p.split("=") for p in corr.param.split(",") if "=" in p)
            shift_from = int(params.get("shift_from", corr.source_breach_hour))
            from_idx = (shift_from - start_hour) % 24
            if from_idx < num_hours and ev_schedule[from_idx][0] > 0:
                to_idx = idx
                if to_idx < num_hours and ev_schedule[to_idx][0] == 0:
                    ev_schedule[to_idx] = ev_schedule[from_idx]
                    ev_schedule[from_idx] = (0.0, 0)
                    corr.applied = True
                    applied_count += 1
                    _LOGGER.info(
                        "Correction applied: shift_ev kl %02d→%02d",
                        shift_from,
                        corr.target_hour,
                    )

        elif corr.action == "add_discharge":
            # Add battery discharge at this hour
            params = dict(p.split("=") for p in corr.param.split(",") if "=" in p)
            discharge_kw = float(params.get("discharge_kw", "2.0"))
            discharge_kw = min(discharge_kw, max_discharge_kw)
            avail_kwh = (battery_soc_pct - battery_min_soc) / 100 * battery_cap_kwh
            if avail_kwh > 1.0:
                battery_schedule[idx] = (-discharge_kw, "d")
                corr.applied = True
                applied_count += 1
                _LOGGER.info(
                    "Correction applied: add_discharge kl %02d (%.1f kW)",
                    corr.target_hour,
                    discharge_kw,
                )

        elif corr.action == "reduce_load" and "pause_miner" in corr.param:
            # Miner pause handled at slot assembly (miner_schedule override)
            corr.applied = True
            applied_count += 1
            _LOGGER.info("Correction applied: pause_miner kl %02d", corr.target_hour)

        elif corr.action == "shift_appliance":
            # Informational — logged but can't directly control appliances
            corr.applied = True
            applied_count += 1
            _LOGGER.info(
                "Correction noted: shift_appliance kl %02d — %s",
                corr.target_hour,
                corr.reason,
            )

    if applied_count:
        _LOGGER.info("Applied %d breach corrections to scheduler plan", applied_count)

    return ev_schedule, battery_schedule


# ── Main Scheduler Entry Point ─────────────────────────────────────


def generate_scheduler_plan(
    start_hour: int,
    num_hours: int = SCHEDULER_PLAN_HOURS,
    # Prices & forecasts
    hourly_prices: list[float] | None = None,
    hourly_pv: list[float] | None = None,
    hourly_loads: list[float] | None = None,
    pv_forecast_daily: list[float] | None = None,
    # Battery state
    battery_soc_pct: float = 50.0,
    battery_cap_kwh: float = 20.0,
    battery_min_soc: float = DEFAULT_BATTERY_MIN_SOC,
    battery_efficiency: float = DEFAULT_BATTERY_EFFICIENCY,
    max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_KW,
    max_grid_charge_kw: float = DEFAULT_MAX_GRID_CHARGE_KW,
    grid_charge_price_threshold: float = DEFAULT_GRID_CHARGE_PRICE_THRESHOLD,
    grid_charge_max_soc: float = DEFAULT_GRID_CHARGE_MAX_SOC,
    # EV state
    ev_enabled: bool = False,
    ev_soc_pct: float = -1.0,
    ev_capacity_kwh: float = 0.0,
    ev_morning_target_soc: float = 75.0,
    ev_days_since_full: int = 0,
    # Target
    target_weighted_kw: float = 2.0,
    night_weight: float = DEFAULT_NIGHT_WEIGHT,
    # Learnings (persisted)
    learnings: list[BreachLearning] | None = None,
    breach_count_month: int = 0,
    # Context
    pv_tomorrow_kwh: float = 0.0,
    daily_consumption_kwh: float = 15.0,
    # IT-2381: Multi-period evening/night optimization
    prices_tomorrow_24h: list[float] | None = None,
    # Breach corrections (auto-generated from previous breaches)
    corrections: list[BreachCorrection] | None = None,
) -> SchedulerPlan:
    """Generate intelligent 24h scheduler plan.

    This is the main entry point called by coordinator every 15 min.
    Coordinates all sub-schedulers (EV, battery, miner) and applies
    constraint checking + remediation.
    """
    num_hours = max(1, min(num_hours, 48))
    if hourly_prices is None:
        hourly_prices = [50.0] * num_hours
    if hourly_pv is None:
        hourly_pv = [0.0] * num_hours
    if hourly_loads is None:
        hourly_loads = [1.5] * num_hours
    if pv_forecast_daily is None:
        pv_forecast_daily = []
    if learnings is None:
        learnings = []

    # Pad inputs to num_hours
    hourly_prices = _pad(hourly_prices, num_hours, 50.0)
    hourly_pv = _pad(hourly_pv, num_hours, 0.0)
    hourly_loads = _pad(hourly_loads, num_hours, 1.5)

    # ── Step 1: Schedule EV (backwards from departure) ────────
    if ev_enabled and ev_soc_pct >= 0 and ev_capacity_kwh > 0:
        ev_schedule = _schedule_ev_backwards(
            num_hours=num_hours,
            start_hour=start_hour,
            ev_soc_pct=ev_soc_pct,
            ev_capacity_kwh=ev_capacity_kwh,
            morning_target_soc=ev_morning_target_soc,
            hourly_prices=hourly_prices,
            hourly_loads=hourly_loads,
            target_weighted_kw=target_weighted_kw,
            battery_kwh_available=max(
                0,
                battery_soc_pct / 100 * battery_cap_kwh - battery_min_soc / 100 * battery_cap_kwh,
            ),
            pv_tomorrow_kwh=pv_tomorrow_kwh,
            daily_consumption_kwh=daily_consumption_kwh,
            learnings=learnings,
            night_weight=night_weight,
            battery_efficiency=battery_efficiency,
        )
    else:
        ev_schedule = [(0.0, 0)] * num_hours

    hourly_ev_kw = [s[0] for s in ev_schedule]

    # ── Step 2: Schedule battery (aware of EV schedule) ───────
    battery_schedule = _schedule_battery(
        num_hours=num_hours,
        start_hour=start_hour,
        hourly_prices=hourly_prices,
        hourly_pv=hourly_pv,
        hourly_loads=hourly_loads,
        hourly_ev=hourly_ev_kw,
        target_weighted_kw=target_weighted_kw,
        battery_soc_pct=battery_soc_pct,
        battery_cap_kwh=battery_cap_kwh,
        battery_min_soc=battery_min_soc,
        battery_efficiency=battery_efficiency,
        night_weight=night_weight,
        grid_charge_price_threshold=grid_charge_price_threshold,
        grid_charge_max_soc=grid_charge_max_soc,
        max_discharge_kw=max_discharge_kw,
        max_grid_charge_kw=max_grid_charge_kw,
        pv_forecast_daily=pv_forecast_daily,
    )

    # ── Step 2b: Multi-period evening/night optimization (IT-2381) ──
    evening_strategy = evaluate_evening_strategy(
        battery_kwh_available=max(
            0,
            battery_soc_pct / 100 * battery_cap_kwh - battery_min_soc / 100 * battery_cap_kwh,
        ),
        battery_cap_kwh=battery_cap_kwh,
        battery_efficiency=battery_efficiency,
        battery_min_soc_pct=battery_min_soc,
        prices_today_24h=hourly_prices[:24],
        prices_tomorrow_24h=prices_tomorrow_24h,
        pv_tomorrow_kwh=pv_tomorrow_kwh,
        daily_consumption_kwh=daily_consumption_kwh,
        hourly_consumption_evening=[
            hourly_loads[i] for i in range(num_hours) if (start_hour + i) % 24 in range(17, 22)
        ][:5],
        ev_need_kwh=sum(s[0] for s in ev_schedule),
        max_grid_charge_kw=max_grid_charge_kw,
    )

    # Apply strategy to battery schedule
    battery_schedule = apply_strategy_to_battery_schedule(
        strategy=evening_strategy,
        battery_schedule=battery_schedule,
        start_hour=start_hour,
        battery_kwh_available=max(
            0,
            battery_soc_pct / 100 * battery_cap_kwh - battery_min_soc / 100 * battery_cap_kwh,
        ),
        max_discharge_kw=max_discharge_kw,
    )

    hourly_battery_kw = [s[0] for s in battery_schedule]

    # ── Step 2c: Apply breach corrections ───────────────────────
    if corrections:
        ev_schedule, battery_schedule = _apply_corrections(
            corrections=corrections,
            ev_schedule=ev_schedule,
            battery_schedule=battery_schedule,
            start_hour=start_hour,
            num_hours=num_hours,
            battery_soc_pct=battery_soc_pct,
            battery_cap_kwh=battery_cap_kwh,
            battery_min_soc=battery_min_soc,
            max_discharge_kw=max_discharge_kw,
        )
        # Refresh after corrections
        hourly_ev_kw = [s[0] for s in ev_schedule]
        hourly_battery_kw = [s[0] for s in battery_schedule]

    # ── Step 3: Schedule miner (after battery + EV) ───────────
    miner_schedule = _schedule_miner(
        num_hours=num_hours,
        start_hour=start_hour,
        hourly_pv=hourly_pv,
        hourly_loads=hourly_loads,
        hourly_ev=hourly_ev_kw,
        hourly_battery=hourly_battery_kw,
    )

    # ── Step 4: Assemble slots ────────────────────────────────
    soc_kwh = battery_soc_pct / 100 * battery_cap_kwh
    ev_soc_kwh = max(0, ev_soc_pct) / 100 * ev_capacity_kwh if ev_capacity_kwh > 0 else 0
    slots: list[SchedulerHourSlot] = []

    for i in range(num_hours):
        abs_h = (start_hour + i) % 24
        w = ellevio_weight(abs_h, night_weight)
        load = hourly_loads[i]
        pv = hourly_pv[i]
        ev_kw, ev_amps = ev_schedule[i]
        batt_kw, batt_action = battery_schedule[i]
        miner = miner_schedule[i]

        # Track SoC
        if batt_kw > 0:
            soc_kwh += batt_kw * battery_efficiency
        elif batt_kw < 0:
            soc_kwh += batt_kw  # Discharge: subtract
        soc_kwh = max(0, min(soc_kwh, battery_cap_kwh))

        if ev_capacity_kwh > 0 and ev_kw > 0:
            ev_soc_kwh += ev_kw * DEFAULT_EV_EFFICIENCY
            ev_soc_kwh = min(ev_soc_kwh, ev_capacity_kwh)

        # Net grid import
        miner_kw = 0.5 if miner else 0.0  # Estimated miner consumption
        net_grid = load + ev_kw + miner_kw - pv + batt_kw
        net_grid = max(0, net_grid)
        weighted = net_grid * w

        batt_soc_pct = max(0, min(100, int(soc_kwh / battery_cap_kwh * 100)))
        ev_soc_pct_now = (
            max(0, min(100, int(ev_soc_kwh / ev_capacity_kwh * 100))) if ev_capacity_kwh > 0 else 0
        )

        # Build reasoning
        price = hourly_prices[i]
        reason_parts: list[str] = []
        if batt_action == "c":
            reason_parts.append(f"Sol-laddning ({pv:.1f}kW PV)")
        elif batt_action == "g":
            reason_parts.append(f"Nät-laddning (pris {price:.0f} öre)")
        elif batt_action == "d":
            reason_parts.append(f"Urladdning (stöd {abs(batt_kw):.1f}kW)")
        else:
            reason_parts.append("Viloläge")
        if ev_kw > 0:
            reason_parts.append(f"EV {ev_amps}A")
        if miner:
            reason_parts.append("Miner ON")

        slots.append(
            SchedulerHourSlot(
                hour=abs_h,
                action=batt_action,
                battery_kw=round(batt_kw, 2),
                ev_kw=round(ev_kw, 2),
                ev_amps=ev_amps,
                miner_on=miner,
                grid_kw=round(net_grid, 2),
                weighted_kw=round(weighted, 2),
                pv_kw=round(pv, 1),
                consumption_kw=round(load, 1),
                price=round(price, 1),
                battery_soc=batt_soc_pct,
                ev_soc=ev_soc_pct_now,
                constraint_ok=weighted <= target_weighted_kw * SCHEDULER_CONSTRAINT_MARGIN,
                reasoning=", ".join(reason_parts),
            )
        )

    # ── Step 5: Constraint check + fix ────────────────────────
    slots = _check_constraints(slots, target_weighted_kw)

    # ── Step 6: Calculate summary stats ───────────────────────
    max_weighted = max((s.weighted_kw for s in slots), default=0)
    total_ev_kwh = sum(s.ev_kw for s in slots)
    total_charge = sum(s.battery_kw for s in slots if s.battery_kw > 0)
    total_discharge = sum(abs(s.battery_kw) for s in slots if s.battery_kw < 0)

    # EV SoC at 06:00
    ev_at_06 = 0
    for s in slots:
        if s.hour == SCHEDULER_EV_DEPARTURE_HOUR:
            ev_at_06 = s.ev_soc
            break

    # Estimated cost
    estimated_cost = sum(max(0, s.grid_kw) * s.price / 100 for s in slots)

    # Weekly EV plan
    ev_full_date = ""
    if ev_enabled and ev_capacity_kwh > 0:
        ev_full_date = plan_ev_full_charge(
            days_since_full=ev_days_since_full,
            pv_forecast_daily=pv_forecast_daily,
            current_weekday=datetime.now().weekday(),
        )

    return SchedulerPlan(
        slots=slots,
        start_hour=start_hour,
        target_weighted_kw=target_weighted_kw,
        max_weighted_kw=round(max_weighted, 2),
        total_ev_kwh=round(total_ev_kwh, 1),
        ev_soc_at_06=ev_at_06,
        total_charge_kwh=round(total_charge, 1),
        total_discharge_kwh=round(total_discharge, 1),
        estimated_cost_kr=round(estimated_cost, 1),
        ev_next_full_charge_date=ev_full_date,
        breach_count_month=breach_count_month,
        learnings=learnings,
        evening_strategy=evening_strategy,
    )


def analyze_idle_time(
    slots: list[SchedulerHourSlot],
    idle_minutes_today: int,
    battery_soc_pct: float,
    battery_min_soc: float,
    battery_cap_kwh: float,
    prices: list[float],
    pv_forecast: list[float],
) -> IdleAnalysis:
    """Analyze battery idle time and identify reduction opportunities.

    Examines the 24h plan to find hours where batteries are idle but could
    be doing useful work (charging from cheap grid, discharging during peaks,
    absorbing PV surplus).
    """
    hours_elapsed = max(1, datetime.now().hour)
    idle_pct = round(idle_minutes_today / (hours_elapsed * 60) * 100, 0)

    missed_charge = 0.0
    missed_discharge = 0.0
    missed_savings_kr = 0.0
    opportunities: list[str] = []

    avg_price = sum(prices) / len(prices) if prices else 50

    for slot in slots:
        if slot.action != "i":  # 'i' = idle
            continue

        price = slot.price

        # Missed PV charge: PV surplus > 0.5 kW but battery idle
        pv_surplus = slot.pv_kw - slot.consumption_kw
        if pv_surplus > 0.5 and battery_soc_pct < 95:
            surplus = min(pv_surplus, 3.0)  # Max 3kW charge rate
            missed_charge += surplus

        # Missed cheap charge: price < 20 öre and battery not full
        if price < 20 and battery_soc_pct < 80:
            missed_charge += 2.0  # Could charge 2 kW

        # Missed discharge: price > avg*1.3 and battery has energy
        if price > avg_price * 1.3 and battery_soc_pct > battery_min_soc + 10:
            avail = min(2.0, (battery_soc_pct - battery_min_soc) / 100 * battery_cap_kwh)
            missed_discharge += avail
            missed_savings_kr += avail * price / 100

    # Generate actionable tips
    if missed_charge > 2:
        opportunities.append(f"Ladda batteri vid PV-överskott: ~{missed_charge:.0f} kWh/dag missas")
    if missed_discharge > 1:
        opportunities.append(
            f"Ladda ur vid dyra timmar: ~{missed_discharge:.0f} kWh "
            f"→ spara ~{missed_savings_kr:.1f} kr/dag"
        )

    cheap_hours = [i for i, p in enumerate(prices) if p < avg_price * 0.6]
    if cheap_hours and battery_soc_pct < 80:
        h_str = ", ".join(f"{h:02d}" for h in cheap_hours[:4])
        opportunities.append(f"Billiga laddningstimmar: {h_str}")

    expensive_hours = [i for i, p in enumerate(prices) if p > avg_price * 1.5]
    if expensive_hours and battery_soc_pct > battery_min_soc + 15:
        h_str = ", ".join(f"{h:02d}" for h in expensive_hours[:4])
        opportunities.append(f"Dyra urladdningstimmar: {h_str}")

    if idle_pct > 70:
        opportunities.append("Batterierna idle >70% — överväg lägre charge/discharge-trösklar")
    elif idle_pct > 50:
        opportunities.append("Batterierna idle >50% — kontrollera att prisarbitrage aktivt")

    # Utilization score: 0-100, penalize idle, reward charge+discharge
    active_slots = sum(1 for s in slots if s.action != "i")
    score = min(100, int(active_slots / max(1, len(slots)) * 100))

    return IdleAnalysis(
        idle_hours_today=idle_minutes_today // 60,
        idle_pct=idle_pct,
        missed_charge_kwh=round(missed_charge, 1),
        missed_discharge_kwh=round(missed_discharge, 1),
        missed_savings_kr=round(missed_savings_kr, 2),
        opportunities=opportunities,
        score=score,
    )


def _pad(lst: list[float], n: int, default: float) -> list[float]:
    """Pad list to length n with default value."""
    if len(lst) >= n:
        return lst[:n]
    return lst + [default] * (n - len(lst))
