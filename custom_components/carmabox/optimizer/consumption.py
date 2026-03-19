"""CARMA Box — Consumption Profile Learning.

Pure Python. No HA imports. Fully testable.

Learns household consumption patterns from historical data.
Separate profiles for weekdays and weekends.
Uses EMA (Exponential Moving Average) for smooth adaptation.
"""

from __future__ import annotations

from datetime import datetime

from ..const import DEFAULT_CONSUMPTION_PROFILE

# EMA alpha: 10% new data, 90% history → smooth but responsive
EMA_ALPHA = 0.1
MIN_SAMPLES_FOR_LEARNED = 168  # 7 days × 24 hours


class ConsumptionProfile:
    """Learned consumption profile with weekday/weekend split.

    Stores 24 hourly values (kW) per day-type.
    Uses EMA to update smoothly as new data arrives.
    """

    def __init__(self) -> None:
        """Initialize with static default profile."""
        self.weekday: list[float] = list(DEFAULT_CONSUMPTION_PROFILE)
        self.weekend: list[float] = list(DEFAULT_CONSUMPTION_PROFILE)
        self.samples_weekday: int = 0
        self.samples_weekend: int = 0

    def update(self, hour: int, consumption_kw: float, is_weekend: bool) -> None:
        """Update profile with a new measurement.

        Args:
            hour: Hour of day (0-23).
            consumption_kw: Measured house consumption (kW).
            is_weekend: True if Saturday or Sunday.
        """
        if hour < 0 or hour > 23:
            return
        # Clamp to reasonable range
        consumption_kw = max(0.0, min(20.0, consumption_kw))

        if is_weekend:
            self.weekend[hour] = EMA_ALPHA * consumption_kw + (1 - EMA_ALPHA) * self.weekend[hour]
            self.samples_weekend += 1
        else:
            self.weekday[hour] = EMA_ALPHA * consumption_kw + (1 - EMA_ALPHA) * self.weekday[hour]
            self.samples_weekday += 1

    def get_profile(self, is_weekend: bool) -> list[float]:
        """Get 24h profile for the given day type.

        Falls back to static DEFAULT_CONSUMPTION_PROFILE until at least
        MIN_SAMPLES_FOR_LEARNED (168 = 7 days × 24h) total samples exist.
        This ensures enough data across both day types before trusting
        the learned profile.
        """
        if self.total_samples < MIN_SAMPLES_FOR_LEARNED:
            return list(DEFAULT_CONSUMPTION_PROFILE)
        if is_weekend:
            return [round(v, 2) for v in self.weekend]
        return [round(v, 2) for v in self.weekday]

    def get_profile_for_date(self, dt: datetime) -> list[float]:
        """Get profile based on date's weekday/weekend."""
        return self.get_profile(dt.weekday() >= 5)

    @property
    def is_learned(self) -> bool:
        """True if enough data for learned profile (vs static default)."""
        return (
            self.samples_weekday >= MIN_SAMPLES_FOR_LEARNED
            or self.samples_weekend >= MIN_SAMPLES_FOR_LEARNED
        )

    @property
    def total_samples(self) -> int:
        """Total number of samples recorded."""
        return self.samples_weekday + self.samples_weekend

    def to_dict(self) -> dict[str, object]:
        """Serialize for storage in config entry options."""
        return {
            "weekday": self.weekday,
            "weekend": self.weekend,
            "samples_weekday": self.samples_weekday,
            "samples_weekend": self.samples_weekend,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ConsumptionProfile:
        """Restore from stored dict."""
        profile = cls()
        weekday = data.get("weekday")
        weekend = data.get("weekend")
        if isinstance(weekday, list) and len(weekday) == 24:
            profile.weekday = [float(v) for v in weekday]
        if isinstance(weekend, list) and len(weekend) == 24:
            profile.weekend = [float(v) for v in weekend]
        sw = data.get("samples_weekday", 0)
        se = data.get("samples_weekend", 0)
        profile.samples_weekday = int(sw) if isinstance(sw, (int, float, str)) else 0
        profile.samples_weekend = int(se) if isinstance(se, (int, float, str)) else 0
        return profile


def calculate_house_consumption(
    grid_power_w: float,
    battery_power_1_w: float,
    battery_power_2_w: float,
    pv_power_w: float,
    ev_power_w: float,
) -> float:
    """Calculate actual house consumption from energy flows.

    House = grid_import + battery_discharge + pv_production - ev_charging
    (All positive values = into house)

    Args:
        grid_power_w: Grid power (positive = import, negative = export).
        battery_power_1_w: Battery 1 power (negative = discharge into house).
        battery_power_2_w: Battery 2 power (negative = discharge into house).
        pv_power_w: PV production (positive = producing).
        ev_power_w: EV charging (positive = consuming from grid/battery).

    Returns:
        House consumption in kW.
    """
    grid_import = max(0, grid_power_w)
    battery_discharge = abs(min(0, battery_power_1_w)) + abs(min(0, battery_power_2_w))
    pv = max(0, pv_power_w)
    ev = max(0, ev_power_w)

    house_w = grid_import + battery_discharge + pv - ev
    return max(0, house_w) / 1000  # Convert to kW
