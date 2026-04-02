"""CARMA Box — Price Pattern Learning (PLAT-963).

Pure Python. No HA imports. Fully testable.

Learns electricity price patterns for:
- Weekday vs weekend profiles
- Monthly/seasonal variation
- Price prediction for days without published Nordpool prices
- Holiday detection (abnormal price patterns)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# EMA alpha for price learning
PRICE_EMA_ALPHA = 0.1
MIN_SAMPLES_FOR_PREDICTION = 14  # 2 weeks


@dataclass
class PriceProfile:
    """Learned electricity price patterns.

    Stores 24h profiles for weekdays and weekends per month.
    Used to predict prices when Nordpool tomorrow data is unavailable.
    """

    # [month][hour] → price (öre/kWh) — weekday
    weekday: dict[int, list[float]] = field(
        default_factory=lambda: {m: [50.0] * 24 for m in range(1, 13)}
    )

    # [month][hour] → price (öre/kWh) — weekend
    weekend: dict[int, list[float]] = field(
        default_factory=lambda: {m: [45.0] * 24 for m in range(1, 13)}
    )

    # Sample counts per month
    weekday_samples: dict[int, int] = field(default_factory=lambda: dict.fromkeys(range(1, 13), 0))
    weekend_samples: dict[int, int] = field(default_factory=lambda: dict.fromkeys(range(1, 13), 0))

    # Daily price records for volatility tracking
    daily_records: list[dict[str, Any]] = field(default_factory=list)

    # Learned spread pattern: [month] → avg daily spread (max-min)
    monthly_spread: dict[int, float] = field(
        default_factory=lambda: dict.fromkeys(range(1, 13), 30.0)
    )

    def record_day(
        self,
        prices_24h: list[float],
        month: int,
        is_weekend: bool,
        date_str: str = "",
    ) -> None:
        """Record one day's 24h prices.

        Args:
            prices_24h: 24 hourly prices (öre/kWh).
            month: Month (1-12).
            is_weekend: True if Saturday or Sunday.
            date_str: ISO date for trend tracking.
        """
        if len(prices_24h) < 24 or month < 1 or month > 12:
            return

        profile = self.weekend[month] if is_weekend else self.weekday[month]
        for h in range(24):
            old = profile[h]
            profile[h] = PRICE_EMA_ALPHA * prices_24h[h] + (1 - PRICE_EMA_ALPHA) * old

        if is_weekend:
            self.weekend_samples[month] += 1
        else:
            self.weekday_samples[month] += 1

        # Track spread
        spread = max(prices_24h) - min(prices_24h)
        old_spread = self.monthly_spread.get(month, 30.0)
        self.monthly_spread[month] = PRICE_EMA_ALPHA * spread + (1 - PRICE_EMA_ALPHA) * old_spread

        # Daily record
        if date_str:
            self.daily_records.append(
                {
                    "date": date_str,
                    "avg": round(sum(prices_24h) / 24, 1),
                    "min": round(min(prices_24h), 1),
                    "max": round(max(prices_24h), 1),
                    "spread": round(spread, 1),
                    "weekend": is_weekend,
                }
            )
            if len(self.daily_records) > 180:
                self.daily_records = self.daily_records[-180:]

    def predict_24h(
        self,
        month: int,
        is_weekend: bool,
        scale_factor: float = 1.0,
    ) -> list[float]:
        """Predict 24h prices based on learned patterns.

        Used when Nordpool tomorrow prices are not yet published.

        Args:
            month: Target month (1-12).
            is_weekend: True if predicting for weekend.
            scale_factor: Optional scaling (e.g., 1.2 for expected higher prices).

        Returns:
            24 predicted prices (öre/kWh).
        """
        if month < 1 or month > 12:
            month = max(1, min(12, month))

        profile = (
            self.weekend.get(month, [50.0] * 24)
            if is_weekend
            else self.weekday.get(month, [50.0] * 24)
        )
        return [round(p * scale_factor, 1) for p in profile]

    def predict_multiday(
        self,
        start_month: int,
        start_weekday: int,
        days: int = 7,
        scale_factor: float = 1.0,
    ) -> list[list[float]]:
        """Predict prices for multiple days.

        Args:
            start_month: Starting month (1-12).
            start_weekday: Starting day of week (0=Monday, 6=Sunday).
            days: Number of days to predict.
            scale_factor: Optional price scaling.

        Returns:
            List of 24h price arrays, one per day.
        """
        result = []
        for d in range(days):
            weekday = (start_weekday + d) % 7
            is_weekend = weekday >= 5
            # Month may advance (simplification: same month)
            prediction = self.predict_24h(start_month, is_weekend, scale_factor)
            result.append(prediction)
        return result

    @property
    def has_sufficient_data(self) -> bool:
        """True if enough data for reliable predictions."""
        total = sum(self.weekday_samples.values()) + sum(self.weekend_samples.values())
        return total >= MIN_SAMPLES_FOR_PREDICTION

    def expected_spread(self, month: int) -> float:
        """Expected daily price spread (max-min) for the given month."""
        return round(self.monthly_spread.get(month, 30.0), 1)

    def charge_threshold(self, month: int, percentile: float = 0.25) -> float:
        """Suggest charge price threshold (öre) based on learned patterns.

        Returns the price at the given percentile of the month's profile.
        """
        if month < 1 or month > 12:
            return 50.0
        # Combine weekday and weekend prices
        all_prices = list(self.weekday.get(month, [50.0] * 24)) + list(
            self.weekend.get(month, [50.0] * 24)
        )
        all_prices.sort()
        idx = int(len(all_prices) * percentile)
        return round(all_prices[idx], 1)

    def discharge_threshold(self, month: int, percentile: float = 0.75) -> float:
        """Suggest discharge price threshold (öre) based on learned patterns."""
        if month < 1 or month > 12:
            return 100.0
        all_prices = list(self.weekday.get(month, [50.0] * 24)) + list(
            self.weekend.get(month, [50.0] * 24)
        )
        all_prices.sort()
        idx = int(len(all_prices) * percentile)
        return round(all_prices[idx], 1)

    def summary(self) -> dict[str, Any]:
        """Summary for diagnostics."""
        total_wd = sum(self.weekday_samples.values())
        total_we = sum(self.weekend_samples.values())
        return {
            "total_weekday_samples": total_wd,
            "total_weekend_samples": total_we,
            "has_sufficient_data": self.has_sufficient_data,
            "monthly_avg_spread": {
                m: round(self.monthly_spread[m], 1)
                for m in range(1, 13)
                if self.weekday_samples.get(m, 0) + self.weekend_samples.get(m, 0) > 0
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "weekday": {str(k): v for k, v in self.weekday.items()},
            "weekend": {str(k): v for k, v in self.weekend.items()},
            "weekday_samples": {str(k): v for k, v in self.weekday_samples.items()},
            "weekend_samples": {str(k): v for k, v in self.weekend_samples.items()},
            "monthly_spread": {str(k): v for k, v in self.monthly_spread.items()},
            "daily_records": self.daily_records[-180:],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriceProfile:
        """Deserialize from storage."""
        p = cls()
        wd = data.get("weekday")
        if isinstance(wd, dict):
            p.weekday = {
                int(k): [float(v) for v in vals]
                for k, vals in wd.items()
                if isinstance(vals, list) and len(vals) == 24
            }
        we = data.get("weekend")
        if isinstance(we, dict):
            p.weekend = {
                int(k): [float(v) for v in vals]
                for k, vals in we.items()
                if isinstance(vals, list) and len(vals) == 24
            }
        wds = data.get("weekday_samples")
        if isinstance(wds, dict):
            p.weekday_samples = {int(k): int(v) for k, v in wds.items()}
        wes = data.get("weekend_samples")
        if isinstance(wes, dict):
            p.weekend_samples = {int(k): int(v) for k, v in wes.items()}
        ms = data.get("monthly_spread")
        if isinstance(ms, dict):
            p.monthly_spread = {int(k): float(v) for k, v in ms.items()}
        dr = data.get("daily_records")
        if isinstance(dr, list):
            p.daily_records = dr[-180:]
        return p
