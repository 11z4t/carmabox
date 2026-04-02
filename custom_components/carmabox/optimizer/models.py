"""CARMA Box — Data models.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BatteryCommand(Enum):
    """Battery command state — replaces fragile string comparison."""

    IDLE = "idle"
    CHARGE_PV = "charge_pv"
    CHARGE_PV_TAPER = "charge_pv_taper"  # IT-1939: BMS taper detection
    BMS_COLD_LOCK = "bms_cold_lock"  # IT-1948: BMS cold lock (cell temp < 10°C)
    STANDBY = "standby"
    DISCHARGE = "discharge"


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
    action: str = "idle"  # charge_pv, charge_pv_taper, discharge, standby, idle, grid_charge
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
    battery_temp_c: float | None = None  # Deprecated - use battery_min_cell_temp_1/2
    battery_min_cell_temp_1: float | None = None  # IT-1948: Battery 1 min cell temp (°C)
    battery_min_cell_temp_2: float | None = None  # IT-1948: Battery 2 min cell temp (°C)

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
        """True if ALL batteries at 99%+. PLAT-948: 1% hysteresis avoids 100 flicker."""
        if self.has_battery_2:
            return self.battery_soc_1 >= 99 and self.battery_soc_2 >= 99
        return self.battery_soc_1 >= 99

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
class SchedulerHourSlot:
    """One hour in the intelligent scheduler plan (IT-2378)."""

    hour: int  # 0-23
    action: str  # 'c'=charge_pv, 'g'=grid_charge, 'd'=discharge, 'i'=idle
    battery_kw: float  # + charge, - discharge
    ev_kw: float  # EV charge power (kW, 0 = off)
    ev_amps: int  # EV amps (0 = off)
    miner_on: bool  # Miner state
    grid_kw: float  # Expected net grid import
    weighted_kw: float  # Ellevio-weighted grid
    pv_kw: float
    consumption_kw: float
    price: float  # öre/kWh
    battery_soc: int  # Expected SoC at end of hour
    ev_soc: int  # Expected EV SoC at end of hour
    constraint_ok: bool  # True if weighted < target * 0.85
    reasoning: str  # Human-readable Swedish explanation


@dataclass
class BreachRecord:
    """Record of an Ellevio target breach for root cause analysis (IT-2378)."""

    timestamp: str  # ISO format
    hour: int
    actual_weighted_kw: float
    target_kw: float
    loads_active: list[str]  # e.g. ['ev:8A', 'dishwasher:2kW', 'house:1.5kW']
    root_cause: str  # Swedish: why it happened
    remediation: str  # Swedish: what to do next time
    severity: str  # 'minor' (<10% over), 'major' (10-25%), 'critical' (>25%)


@dataclass
class BreachLearning:
    """Learned pattern from breaches to avoid repetition (IT-2378)."""

    pattern: str  # e.g. 'ev+dishwasher_23'
    hour: int
    description: str  # Swedish
    action: str  # e.g. 'pause_ev', 'reduce_ev_amps', 'shift_ev'
    confidence: float  # 0.0-1.0 (increases with repeated breaches)
    occurrences: int


@dataclass
class BreachCorrection:
    """Automatic correction generated after breach (applied to prevent recurrence)."""

    created: str  # ISO timestamp
    source_breach_hour: int  # Hour when breach occurred
    action: str  # 'reduce_ev', 'shift_ev', 'add_discharge', 'reduce_load', 'shift_appliance'
    target_hour: int  # Hour to apply correction
    param: str  # e.g. 'ev_amps=6', 'discharge_kw=2', 'pause_miner'
    reason: str  # Swedish — why this correction
    applied: bool = False  # Set to True when scheduler uses it
    expired: bool = False  # Set to True after 24h


@dataclass
class HourlyMeterState:
    """Rolling state for hourly Ellevio meter tracking."""

    hour: int = -1
    samples: list[float] = field(default_factory=list)  # Weighted kW samples (30s each)
    projected_avg: float = 0.0  # Where this hour will end up
    warning_issued: bool = False  # 80% warning sent
    load_shed_active: bool = False  # Emergency load shedding active
    peak_sample: float = 0.0  # Highest sample this hour


@dataclass
class SchedulerPlan:
    """Complete 24h scheduler plan (IT-2378)."""

    slots: list[SchedulerHourSlot] = field(default_factory=list)
    start_hour: int = 0
    target_weighted_kw: float = 2.0
    max_weighted_kw: float = 0.0
    total_ev_kwh: float = 0.0
    ev_soc_at_06: int = 0
    total_charge_kwh: float = 0.0
    total_discharge_kwh: float = 0.0
    estimated_cost_kr: float = 0.0
    ev_next_full_charge_date: str = ""  # ISO date
    breaches: list[BreachRecord] = field(default_factory=list)
    breach_count_month: int = 0
    learnings: list[BreachLearning] = field(default_factory=list)
    evening_strategy: MultiPeriodStrategy | None = None  # IT-2381
    idle_analysis: IdleAnalysis | None = None  # Battery idle reduction


@dataclass
class IdleAnalysis:
    """Analysis of battery idle time with reduction recommendations."""

    idle_hours_today: int = 0  # Hours batteries were idle today
    idle_pct: float = 0.0  # Idle % of day so far
    missed_charge_kwh: float = 0.0  # PV that went to export instead of battery
    missed_discharge_kwh: float = 0.0  # Expensive hours where battery could've discharged
    missed_savings_kr: float = 0.0  # SEK lost to idle time
    opportunities: list[str] = field(default_factory=list)  # Reduction tips
    score: int = 0  # 0-100 utilization score (100=fully utilized)


@dataclass
class MultiPeriodStrategy:
    """Result of multi-period evening/night optimization (IT-2381).

    Compares: (A) discharge battery evening + grid recharge night
    vs (B) save battery evening + use tomorrow.
    """

    # Strategy choice
    chosen: str = "A"  # 'A' = discharge+recharge, 'B' = save for tomorrow
    confidence: float = 0.0  # 0-1, higher = more confident

    # Strategy A: discharge evening + grid recharge night
    a_evening_savings_kr: float = 0.0  # Avoided grid import evening
    a_night_recharge_cost_kr: float = 0.0  # Grid charge cost at night
    a_ev_night_cost_kr: float = 0.0  # EV grid charge cost
    a_total_cost_kr: float = 0.0  # Net cost of strategy A

    # Strategy B: save battery + use tomorrow
    b_evening_import_cost_kr: float = 0.0  # Grid import cost evening
    b_tomorrow_savings_kr: float = 0.0  # Battery saves tomorrow (peak avoidance)
    b_ev_night_cost_kr: float = 0.0  # EV grid charge cost (same)
    b_total_cost_kr: float = 0.0  # Net cost of strategy B

    # Decision inputs
    battery_kwh_available: float = 0.0
    evening_avg_price_ore: float = 0.0
    night_avg_price_ore: float = 0.0
    tomorrow_peak_price_ore: float = 0.0
    pv_tomorrow_kwh: float = 0.0
    ev_need_kwh: float = 0.0
    reasoning: str = ""  # Swedish explanation


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
