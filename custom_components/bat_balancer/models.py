"""Pure dataclasses for bat_balancer — no HA dependency (spec BAL-10-BAT §4)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .const import (
    FORRAD_MAX_CHARGE_W,
    FORRAD_MAX_DISCHARGE_W,
    KONTOR_MAX_CHARGE_W,
    KONTOR_MAX_DISCHARGE_W,
    SOC_EQ_MAX_BIAS_DEFAULT_W,
    SOC_EQ_THRESHOLD_DEFAULT_PCT,
    BatBalancerStatus,
    RejectedReason,
)

_STATIC_MAX_CHARGE: dict[str, float] = {
    "kontor": KONTOR_MAX_CHARGE_W,
    "forrad": FORRAD_MAX_CHARGE_W,
}
_STATIC_MAX_DISCHARGE: dict[str, float] = {
    "kontor": KONTOR_MAX_DISCHARGE_W,
    "forrad": FORRAD_MAX_DISCHARGE_W,
}


@dataclass(frozen=True)
class BankConfig:
    """Static per-bank hardware config (from hw-bindings/9X-bat-*.yaml)."""

    id: str
    capacity_kwh: float
    max_charge_w: float
    max_discharge_w: float
    min_soc_pct: float = 5.0
    efficiency: float = 0.95

    @classmethod
    def default_kontor(cls) -> BankConfig:
        return cls(
            id="kontor",
            capacity_kwh=14.0,
            max_charge_w=KONTOR_MAX_CHARGE_W,
            max_discharge_w=KONTOR_MAX_DISCHARGE_W,
        )

    @classmethod
    def default_forrad(cls) -> BankConfig:
        return cls(
            id="forrad",
            capacity_kwh=6.0,
            max_charge_w=FORRAD_MAX_CHARGE_W,
            max_discharge_w=FORRAD_MAX_DISCHARGE_W,
        )


@dataclass
class BankState:
    """Mutable live state for one battery bank (read each tick from HA sensors)."""

    bank_id: str
    current_soc: float = 50.0  # 0-100 %
    is_online: bool = True
    battery_mode: str = "battery_standby"  # charge_battery | discharge_battery | battery_standby
    bms_max_charge_w: float | None = None  # None = use static from BankConfig
    bms_max_discharge_w: float | None = None
    sensor_stale: bool = False  # True if SoC sensor.last_changed > HW_STALE_THRESHOLD_S


@dataclass(frozen=True)
class SensorSnapshot:
    """Immutable snapshot of all HA sensor values read in one tick."""

    brain_target_bat_w: float = 0.0
    banks: dict[str, BankState] = field(default_factory=dict)
    house_grid_w: float = 0.0  # positive = import, negative = export
    pv_w: float = 0.0
    soc_equalization_threshold_pct: float = SOC_EQ_THRESHOLD_DEFAULT_PCT
    soc_equalization_max_bias_w: float = SOC_EQ_MAX_BIAS_DEFAULT_W
    shadow_mode: bool = True
    brain_target_available: bool = True


@dataclass(frozen=True)
class DistributionResult:
    """Immutable result from distribute_target_to_banks() (spec §7)."""

    targets: dict[str, float]  # bank_id → signed target W
    actual_total_w: float  # Σ abs(targets) for online banks
    rejected_w: float = 0.0
    rejected_reason: RejectedReason | None = None
    status: BatBalancerStatus = BatBalancerStatus.OK
    zg12_engaged: bool = False
    equalization_active: bool = False
    equalization_bias_max_w: float = 0.0
    inv23_violation: bool = False  # logged as WARNING, not crash
    overflow_redistributed: bool = False


@dataclass
class BatBalancerState:
    """Mutable runtime state persisted across ticks."""

    status: BatBalancerStatus = BatBalancerStatus.INITIALIZING
    last_target_w: float = 0.0
    last_distribution: dict[str, float] = field(default_factory=dict)
    bms_cap_history: deque = field(
        default_factory=lambda: deque(maxlen=6)  # 6 × 5s = 30s history
    )

    # Metrics (rolling counters — coordinator resets at 24h boundary)
    transitions_24h: int = 0
    zg12_count_24h: int = 0
    overflow_count_24h: int = 0
    equalization_active: bool = False

    # Sign-machine state (owned by SignStateMachine — mirrored here for sensors)
    sign_flip_pending: bool = False
