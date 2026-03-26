"""CARMA Box — Intelligent Scheduler (IT-2378).

Pure Python. No HA imports. Fully testable.
Generates hour-by-hour plan optimized for 6 goals.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)
NIGHT_START, NIGHT_END, NIGHT_WEIGHT = 22, 6, 0.5


@dataclass
class HourSlot:
    hour: int
    price_ore: float = 0.0
    pv_kw: float = 0.0
    house_kw: float = 2.0
    appliance_risk: bool = False
    weight: float = 1.0
    ev_amps: int = 0
    ev_kw: float = 0.0
    bat_action: str = "idle"
    bat_kw: float = 0.0
    miner: bool = False
    grid_kw: float = 0.0
    weighted_kw: float = 0.0
    ok: bool = True


@dataclass
class ScheduleInput:
    hour: int
    prices: list
    pv: list
    house: list
    ev_soc: float = 50.0
    ev_target: float = 75.0
    ev_cap: float = 87.5
    ev_connected: bool = False
    ev_phases: int = 3
    bat_soc: float = 50.0
    bat_cap: float = 20.0
    bat_min: float = 15.0
    bat_cold: bool = False
    bat_reserve: float = 0.0
    target_day: float = 2.0
    target_night: float = 4.0
    disk_hours: list = field(default_factory=lambda: [23, 0])


def _detect_season_mode(inp: ScheduleInput) -> str:
    """Detect season mode from solar forecast.
    
    summer: avg PV > 25 kWh/day → batteries fill daily, aggressive night discharge
    winter: avg PV < 10 kWh/day → conserve battery, grid charge at cheap hours  
    transition: between → balanced
    """
    total_pv = sum(inp.pv) if inp.pv else 0
    if total_pv > 25:
        return "summer"
    elif total_pv < 10:
        return "winter"
    return "transition"


def generate(inp: ScheduleInput) -> list:
    slots = []
    for i in range(24):
        h = (inp.hour + i) % 24
        is_night = h >= NIGHT_START or h < NIGHT_END
        slots.append(HourSlot(
            hour=h,
            price_ore=inp.prices[i] if i < len(inp.prices) else 50,
            pv_kw=inp.pv[i] if i < len(inp.pv) else 0,
            house_kw=inp.house[i] if i < len(inp.house) else 2,
            appliance_risk=h in inp.disk_hours,
            weight=NIGHT_WEIGHT if is_night else 1.0,
        ))

    if inp.ev_connected and inp.ev_soc < inp.ev_target:
        _plan_ev(slots, inp)

    if not inp.bat_cold:
        _plan_battery(slots, inp)

    for s in slots:
        _calc(s, inp)

    _enforce(slots, inp)
    return slots


def _plan_ev(slots, inp):
    need = (inp.ev_target - inp.ev_soc) / 100 * inp.ev_cap
    if need < 0.5:
        return
    kw = 6 * 230 * inp.ev_phases / 1000
    night = [s for s in slots if s.hour >= NIGHT_START or s.hour < NIGHT_END]
    safe = [s for s in night if not s.appliance_risk]
    pool = safe if len(safe) >= need / kw + 0.5 else night
    pool.sort(key=lambda s: s.price_ore)
    n = min(len(pool), int(need / kw + 1))
    rem = need
    for s in sorted(pool[:n], key=lambda s: -s.hour):
        if rem <= 0:
            break
        s.ev_amps = 6
        s.ev_kw = min(kw, rem)
        rem -= s.ev_kw


def _plan_battery(slots, inp):
    avail = max(0, (inp.bat_soc - inp.bat_min) / 100 * inp.bat_cap - inp.bat_reserve)
    prices = [s.price_ore for s in slots if s.price_ore > 0]
    if not prices or avail < 1:
        return
    avg = sum(prices) / len(prices)
    for s in sorted(slots, key=lambda x: -x.price_ore):
        if avail <= 0:
            break
        if s.price_ore > max(avg * 1.3, 50) and s.pv_kw < 1:
            s.bat_action = "discharge"
            s.bat_kw = -min(2, avail)
            avail -= 2
    for s in slots:
        if s.pv_kw > s.house_kw + 0.5 and s.bat_action == "idle":
            s.bat_action = "charge_pv"
            s.bat_kw = min(s.pv_kw - s.house_kw, 3)


def _calc(s, inp):
    miner = 0.4 if s.miner else 0
    app = 1.5 if s.appliance_risk else 0
    charge = max(0, s.bat_kw)
    discharge = abs(min(0, s.bat_kw))
    grid = s.house_kw + app + s.ev_kw + miner + charge - s.pv_kw - discharge
    s.grid_kw = max(0, grid)
    s.weighted_kw = s.grid_kw * s.weight
    target = inp.target_night if s.weight < 1 else inp.target_day
    s.ok = s.weighted_kw <= target


def _enforce(slots, inp):
    """Resolve constraint violations using multi-option evaluation.
    
    Priority: (1) miner off, (2) battery support, (3) reduce EV, (4) pause EV.
    Battery support preferred over EV reduction when it keeps all goals met.
    """
    ev_kw_rate = 6 * 230 * inp.ev_phases / 1000
    ev_need = max(0, (inp.ev_target - inp.ev_soc) / 100 * inp.ev_cap)
    ev_slots = sum(1 for s in slots if s.ev_amps > 0)
    ev_can_lose_hours = max(0, ev_slots - int(ev_need / ev_kw_rate))

    bat_avail = max(0, (inp.bat_soc - inp.bat_min) / 100 * inp.bat_cap - inp.bat_reserve)

    for _ in range(10):
        bad = [s for s in slots if not s.ok]
        if not bad:
            return
        for s in bad:
            target = inp.target_night if s.weight < 1 else inp.target_day
            excess = s.weighted_kw - target

            # Option 1: Turn off miner (free, no goal impact)
            if s.miner:
                s.miner = False
                _calc(s, inp)
                if s.ok:
                    continue

            # Option 2: Battery discharge support (preserves EV charging)
            if not inp.bat_cold and bat_avail > 0.5 and s.bat_action != "discharge":
                needed_discharge = excess / s.weight if s.weight > 0 else excess
                discharge = min(3.0, needed_discharge + 0.5, bat_avail)
                s.bat_action = "discharge"
                s.bat_kw = -discharge
                bat_avail -= discharge
                _calc(s, inp)
                if s.ok:
                    continue

            # Option 3: Reduce EV (only if we can still reach target)
            if s.ev_amps > 0 and ev_can_lose_hours > 0:
                s.ev_amps = 0
                s.ev_kw = 0
                ev_can_lose_hours -= 1
                _calc(s, inp)
                if s.ok:
                    continue

            # Option 4: Stop battery charging
            if s.bat_kw > 0:
                s.bat_action = "idle"
                s.bat_kw = 0
                _calc(s, inp)

            # Option 5: Force EV stop (last resort)
            if not s.ok and s.ev_amps > 0:
                s.ev_amps = 0
                s.ev_kw = 0
                _calc(s, inp)
