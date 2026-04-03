"""CARMA Box — Device Profiles for energy optimization.

Pure Python. No HA imports. Fully testable.

Defines device profiles, load slots, scenarios, and factory functions
used by the optimizer and scheduler to model energy consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..const import (
    DEFAULT_BAT_MIN_CHARGE_W,
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_EV_EFFICIENCY,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_SURPLUS_MINER_W,
    DEFAULT_SURPLUS_POOL_HEATER_W,
    DEFAULT_SURPLUS_VP_KONTOR_W,
    DEFAULT_SURPLUS_VP_POOL_W,
    DEFAULT_VOLTAGE,
    DISHWASHER_AVG_KW,
    DISHWASHER_COOLDOWN_MIN,
    DISHWASHER_PEAK_KW,
    DISHWASHER_RUNTIME_H,
    EV_DAILY_ROLLING_DAYS,
)

# ── Module-level defaults (device-specific hardware specs) ─────────────────

# EV power range derived from amps x phases x voltage
_EV_MIN_KW: float = DEFAULT_EV_MIN_AMPS * 3 * DEFAULT_VOLTAGE / 1000.0
_EV_MAX_KW: float = DEFAULT_EV_MAX_AMPS * 3 * DEFAULT_VOLTAGE / 1000.0
_EV_CAPACITY_KWH: float = 82.0  # XPENG G9 usable battery capacity

# Battery charge rates (GoodWe inverter hardware specs)
_BATTERY_KONTOR_CHARGE_KW: float = 3.6  # GoodWe 3600 single-phase (kontor)
_BATTERY_FORRAD_CHARGE_KW: float = 1.8  # GoodWe 1800 single-phase (förråd)
_BAT_MIN_KW: float = DEFAULT_BAT_MIN_CHARGE_W / 1000.0


def _now() -> datetime:
    return datetime.now()


# ── DeviceProfile ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeviceProfile:
    """Describes an energy consumer for scheduling and optimization.

    frozen=True prevents field reassignment. The _daily_consumption_samples
    list is intentionally mutable (append/pop via methods) even in a frozen
    dataclass — Python only prevents attribute reassignment, not list mutation.
    """

    name: str
    display_name: str
    power_kw: float
    min_power_kw: float
    max_power_kw: float
    min_runtime_h: float
    interruptible: bool
    cooldown_min: int
    priority: int
    consumer_type: str  # "variable" | "on_off" | "climate"
    efficiency: float
    entity_switch: str | None
    entity_power: str | None
    capacity_kwh: float | None
    _daily_consumption_samples: list[float] = field(
        default_factory=list, hash=False, compare=False, repr=False
    )

    def energy_needed(self, current_pct: float, target_pct: float) -> float:
        """Return AC kWh needed to charge from current_pct to target_pct.

        Returns 0.0 if already at or above target, or if capacity is unknown.
        Formula: (target - current) / 100 * capacity_kwh / efficiency
        """
        if self.capacity_kwh is None or target_pct <= current_pct:
            return 0.0
        return (target_pct - current_pct) / 100.0 * self.capacity_kwh / self.efficiency

    def update_daily_consumption(self, kwh: float) -> None:
        """Append a daily consumption sample; keep rolling window of EV_DAILY_ROLLING_DAYS."""
        self._daily_consumption_samples.append(kwh)
        while len(self._daily_consumption_samples) > EV_DAILY_ROLLING_DAYS:
            self._daily_consumption_samples.pop(0)

    def avg_daily_consumption(self) -> float:
        """Return average of rolling consumption samples, or 0.0 if empty."""
        if not self._daily_consumption_samples:
            return 0.0
        return sum(self._daily_consumption_samples) / len(self._daily_consumption_samples)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for sensors and logging."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "power_kw": self.power_kw,
            "min_power_kw": self.min_power_kw,
            "max_power_kw": self.max_power_kw,
            "min_runtime_h": self.min_runtime_h,
            "interruptible": self.interruptible,
            "cooldown_min": self.cooldown_min,
            "priority": self.priority,
            "consumer_type": self.consumer_type,
            "efficiency": self.efficiency,
            "entity_switch": self.entity_switch,
            "entity_power": self.entity_power,
            "capacity_kwh": self.capacity_kwh,
            "avg_daily_consumption_kwh": self.avg_daily_consumption(),
        }


# ── LoadSlot ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoadSlot:
    """A scheduled load assignment for a specific hour."""

    hour: int
    device: str
    power_kw: float
    duration_min: int = 60
    reason: str = ""


# ── Scenario ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    """A complete scheduling scenario for comparison and cost scoring."""

    name: str
    slots: list[LoadSlot] = field(default_factory=list, hash=False, compare=False)
    total_cost_kr: float = 0.0
    ev_target_soc: float = 0.0
    battery_target_soc: float = 0.0
    created_at: datetime = field(default_factory=_now)

    @property
    def total_energy_kwh(self) -> float:
        """Total scheduled energy across all slots (kWh)."""
        return sum(s.power_kw * s.duration_min / 60.0 for s in self.slots)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage and comparison."""
        return {
            "name": self.name,
            "total_cost_kr": self.total_cost_kr,
            "total_energy_kwh": self.total_energy_kwh,
            "ev_target_soc": self.ev_target_soc,
            "battery_target_soc": self.battery_target_soc,
            "created_at": self.created_at.isoformat(),
            "slots": [
                {
                    "hour": s.hour,
                    "device": s.device,
                    "power_kw": s.power_kw,
                    "duration_min": s.duration_min,
                    "reason": s.reason,
                }
                for s in self.slots
            ],
        }


# ── Mutual exclusion ──────────────────────────────────────────────────────


def can_coexist(a: DeviceProfile, b: DeviceProfile) -> bool:
    """Return False if a and b cannot run simultaneously.

    Mutual exclusion matrix:
    - EV + battery_*: one heavy load at a time (grid_charge conflict)
    - dishwasher + EV: one heavy load at a time
    - dishwasher + battery_*: grid charge conflict
    All other combinations return True.
    """
    names = frozenset({a.name, b.name})
    has_battery = any(n.startswith("battery_") for n in names)

    if "ev" in names and has_battery:
        return False
    if "dishwasher" in names and "ev" in names:
        return False
    return not ("dishwasher" in names and has_battery)


# ── Factory ───────────────────────────────────────────────────────────────


def build_profiles(config: dict[str, Any]) -> dict[str, DeviceProfile]:
    """Build all device profiles, applying config overrides on top of defaults.

    Config structure (all keys optional):
        {
            "ev": {"entity_switch": "switch.easee", "entity_power": "sensor.ev_power"},
            "battery_kontor": {"entity_switch": "switch.goodwe_k"},
            ...
        }
    Any DeviceProfile field can be overridden per device.
    """
    defaults: dict[str, dict[str, Any]] = {
        "ev": {
            "display_name": "XPENG G9",
            "power_kw": _EV_MAX_KW,
            "min_power_kw": _EV_MIN_KW,
            "max_power_kw": _EV_MAX_KW,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 1,
            "consumer_type": "variable",
            "efficiency": DEFAULT_EV_EFFICIENCY,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": _EV_CAPACITY_KWH,
        },
        "battery_kontor": {
            "display_name": "GoodWe Kontor",
            "power_kw": _BATTERY_KONTOR_CHARGE_KW,
            "min_power_kw": _BAT_MIN_KW,
            "max_power_kw": _BATTERY_KONTOR_CHARGE_KW,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 2,
            "consumer_type": "variable",
            "efficiency": DEFAULT_BATTERY_EFFICIENCY,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": DEFAULT_BATTERY_1_KWH,
        },
        "battery_forrad": {
            "display_name": "GoodWe Förråd",
            "power_kw": _BATTERY_FORRAD_CHARGE_KW,
            "min_power_kw": _BAT_MIN_KW,
            "max_power_kw": _BATTERY_FORRAD_CHARGE_KW,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 2,
            "consumer_type": "variable",
            "efficiency": DEFAULT_BATTERY_EFFICIENCY,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": DEFAULT_BATTERY_2_KWH,
        },
        "vp_kontor": {
            "display_name": "VP Kontor",
            "power_kw": DEFAULT_SURPLUS_VP_KONTOR_W / 1000.0,
            "min_power_kw": DEFAULT_SURPLUS_VP_KONTOR_W / 1000.0,
            "max_power_kw": DEFAULT_SURPLUS_VP_KONTOR_W / 1000.0,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 3,
            "consumer_type": "climate",
            "efficiency": 1.0,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": None,
        },
        "vp_pool": {
            "display_name": "VP Pool",
            "power_kw": DEFAULT_SURPLUS_VP_POOL_W / 1000.0,
            "min_power_kw": DEFAULT_SURPLUS_VP_POOL_W / 1000.0,
            "max_power_kw": DEFAULT_SURPLUS_VP_POOL_W / 1000.0,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 4,
            "consumer_type": "on_off",
            "efficiency": 1.0,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": None,
        },
        "pool_heater": {
            "display_name": "Pool Heater",
            "power_kw": DEFAULT_SURPLUS_POOL_HEATER_W / 1000.0,
            "min_power_kw": DEFAULT_SURPLUS_POOL_HEATER_W / 1000.0,
            "max_power_kw": DEFAULT_SURPLUS_POOL_HEATER_W / 1000.0,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 5,
            "consumer_type": "on_off",
            "efficiency": 1.0,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": None,
        },
        "miner": {
            "display_name": "Miner",
            "power_kw": DEFAULT_SURPLUS_MINER_W / 1000.0,
            "min_power_kw": DEFAULT_SURPLUS_MINER_W / 1000.0,
            "max_power_kw": DEFAULT_SURPLUS_MINER_W / 1000.0,
            "min_runtime_h": 0.0,
            "interruptible": True,
            "cooldown_min": 0,
            "priority": 6,
            "consumer_type": "on_off",
            "efficiency": 1.0,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": None,
        },
        "dishwasher": {
            "display_name": "Dishwasher",
            "power_kw": DISHWASHER_AVG_KW,
            "min_power_kw": DISHWASHER_AVG_KW,
            "max_power_kw": DISHWASHER_PEAK_KW,
            "min_runtime_h": DISHWASHER_RUNTIME_H,
            "interruptible": False,
            "cooldown_min": DISHWASHER_COOLDOWN_MIN,
            "priority": 7,
            "consumer_type": "on_off",
            "efficiency": 1.0,
            "entity_switch": None,
            "entity_power": None,
            "capacity_kwh": None,
        },
    }

    profiles: dict[str, DeviceProfile] = {}
    for name, spec in defaults.items():
        overrides: dict[str, Any] = config.get(name) or {}
        merged: dict[str, Any] = {**spec, **overrides}
        profiles[name] = DeviceProfile(name=name, **merged)

    return profiles
