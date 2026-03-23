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
class ShadowComparison:
    """Shadow mode: what CARMA would do vs what v6 actually does."""

    timestamp: str = ""
    carma_action: str = "idle"
    actual_action: str = "idle"  # Derived from battery power direction
    carma_discharge_w: int = 0
    actual_discharge_w: int = 0
    carma_weighted_kw: float = 0.0
    actual_weighted_kw: float = 0.0
    price_ore: float = 0.0
    agreement: bool = True  # True if CARMA and v6 agree
    carma_better_kr: float = 0.0  # Positive = CARMA would save more
    reason: str = ""


@dataclass
class CarmaboxState:
    """Current state of the entire system."""

    # Grid
    grid_power_w: float = 0.0

    # Battery 1 (primary)
    battery_soc_1: float = 0.0
    battery_power_1: float = 0.0
    battery_power_1_valid: bool = True  # False = unknown/unavailable at HA start (PLAT-946)
    battery_ems_1: str = ""

    battery_cap_1_kwh: float = 15.0

    # Battery 2 (optional, -1 = not present)
    battery_soc_2: float = -1.0
    battery_power_2: float = 0.0
    battery_power_2_valid: bool = True  # False = unknown/unavailable at HA start (PLAT-946)
    battery_ems_2: str = ""
    battery_cap_2_kwh: float = 5.0

    # PV
    pv_power_w: float = 0.0

    # EV (-1 = not present)
    ev_soc: float = -1.0
    ev_power_w: float = 0.0
    ev_current_a: float = 0.0
    ev_status: str = ""

    # Temperature
    battery_temp_c: float | None = None

    # Weather (Tempest)
    outdoor_temp_c: float = 0.0
    solar_radiation_wm2: float = 0.0  # W/m² — direct solar irradiance
    illuminance_lx: float = 0.0  # lux — light level
    barometric_pressure_hpa: float = 0.0  # hPa — falling = bad weather coming
    rain_mm: float = 0.0  # mm last hour — rain = no PV
    wind_speed_kmh: float = 0.0  # km/h — affects VP efficiency

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
        """True if ALL batteries at 100%. Push last % with PV when available."""
        if self.has_battery_2:
            return self.battery_soc_1 >= 100 and self.battery_soc_2 >= 100
        return self.battery_soc_1 >= 100

    @property
    def total_battery_soc(self) -> float:
        """Capacity-weighted SoC across all batteries."""
        if self.has_battery_2:
            total_cap = self.battery_cap_1_kwh + self.battery_cap_2_kwh
            if total_cap > 0:
                return (
                    self.battery_soc_1 * self.battery_cap_1_kwh
                    + self.battery_soc_2 * self.battery_cap_2_kwh
                ) / total_cap
            return (self.battery_soc_1 + self.battery_soc_2) / 2
        return self.battery_soc_1


@dataclass
class HouseholdProfile:
    """Household metadata for benchmarking and energy advisory.

    Collected during config flow. Anonymized before hub sync.
    """

    # House
    house_size_m2: int = 0
    heating_type: str = ""  # fjv, vp, direct, other
    has_hot_water_heater: bool = False

    # Solar
    solar_kwp: float = 0.0
    solar_direction: str = ""  # S, SO, SV, O, V, N
    solar_tilt: int = 0  # degrees

    # Battery
    battery_brand: str = ""
    battery_count: int = 0
    battery_total_kwh: float = 0.0

    # EV
    ev_brand: str = ""
    ev_capacity_kwh: float = 0.0
    ev_charge_speed_kw: float = 0.0

    # Location (anonymized — no street address)
    postal_code: str = ""  # 5-digit Swedish
    municipality: str = ""
    price_area: str = ""  # SE1-SE4
    grid_operator: str = ""

    # Electricity contract
    contract_type: str = ""  # fixed, variable
    electricity_retailer: str = ""
    grid_fee_kr_per_kw: float = 0.0


@dataclass
class BenchmarkData:
    """Benchmarking comparison data from hub."""

    # Population
    similar_households: int = 0
    comparison_group: str = ""  # e.g. "Villa 120-160m², VP, SE3"

    # Consumption comparison
    your_monthly_kwh: float = 0.0
    avg_monthly_kwh: float = 0.0
    diff_pct: float = 0.0  # Positive = you use more than average
    trend_3m: str = ""  # "improving", "stable", "increasing"

    # Savings comparison
    your_savings_kr: float = 0.0
    avg_savings_kr: float = 0.0
    savings_rank_pct: float = 0.0  # Percentile (top 10% = 90)

    # Tips
    tips: list[str] = field(default_factory=list)

    # ROI
    battery_roi_months: int = 0
    solar_roi_months: int = 0

    # Timestamp
    updated: str = ""
