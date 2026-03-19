"""CARMA Box — Data models.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HourPlan:
    """Plan for a single hour."""

    hour: int
    action: str  # 'c' = charge, 'd' = discharge, 'i' = idle
    battery_kw: float  # + charge, - discharge
    grid_kw: float  # Expected grid import
    weighted_kw: float  # Ellevio-weighted
    pv_kw: float
    consumption_kw: float
    ev_kw: float
    ev_soc: int
    battery_soc: int
    price: float


@dataclass
class PlanSummary:
    """Summary of a plan."""

    max_weighted_kw: float
    total_charge_kwh: float
    total_discharge_kwh: float
    total_ev_kwh: float
    ev_soc_at_06: int | None
    estimated_cost_kr: float
    hours_planned: int
    start_hour: int


@dataclass
class HourActual:
    """Actual values for a completed hour (plan vs reality)."""

    hour: int = 0
    planned_action: str = "i"
    actual_action: str = "i"
    planned_grid_kw: float = 0.0
    actual_grid_kw: float = 0.0
    planned_weighted_kw: float = 0.0
    actual_weighted_kw: float = 0.0
    planned_battery_soc: int = 0
    actual_battery_soc: int = 0
    planned_ev_soc: int = 0
    actual_ev_soc: int = 0
    price: float = 0.0


@dataclass
class Decision:
    """A single optimizer decision with reasoning."""

    timestamp: str = ""
    action: str = "idle"  # charge_pv, discharge, standby, idle, grid_charge
    reason: str = ""  # Human-readable Swedish
    target_kw: float = 0.0
    grid_kw: float = 0.0
    weighted_kw: float = 0.0
    price_ore: float = 0.0
    battery_soc: float = 0.0
    ev_soc: float = -1.0
    pv_kw: float = 0.0
    discharge_w: int = 0
    ev_amps: int = 0
    battery_support_kwh: float = 0.0
    safety_blocked: bool = False
    safety_reason: str = ""
    reasoning: list[str] = field(default_factory=list)
    reasoning_chain: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CarmaboxState:
    """Current state of the entire system."""

    # Grid
    grid_power_w: float = 0.0

    # Battery 1 (primary)
    battery_soc_1: float = 0.0
    battery_power_1: float = 0.0
    battery_ems_1: str = ""

    # Battery 2 (optional, -1 = not present)
    battery_soc_2: float = -1.0
    battery_power_2: float = 0.0
    battery_ems_2: str = ""

    # PV
    pv_power_w: float = 0.0

    # EV (-1 = not present)
    ev_soc: float = -1.0
    ev_power_w: float = 0.0
    ev_current_a: float = 0.0
    ev_status: str = ""

    # Temperature
    battery_temp_c: float | None = None

    # Price
    current_price: float = 0.0

    # Computed
    target_weighted_kw: float = 2.0
    plan: list[HourPlan] = field(default_factory=list)

    @property
    def is_exporting(self) -> bool:
        """True if we're exporting to grid."""
        return self.grid_power_w < 0

    @property
    def has_battery_2(self) -> bool:
        """True if second battery exists."""
        return self.battery_soc_2 >= 0

    @property
    def has_ev(self) -> bool:
        """True if EV charger exists."""
        return self.ev_soc >= 0

    @property
    def all_batteries_full(self) -> bool:
        """True if all batteries at 100%."""
        if self.has_battery_2:
            return self.battery_soc_1 >= 100 and self.battery_soc_2 >= 100
        return self.battery_soc_1 >= 100

    @property
    def total_battery_soc(self) -> float:
        """Average SoC across all batteries."""
        if self.has_battery_2:
            return (self.battery_soc_1 + self.battery_soc_2) / 2
        return self.battery_soc_1
