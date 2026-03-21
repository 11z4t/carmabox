"""CARMA Box — PV Forecast Correction (PLAT-963).

Pure Python. No HA imports. Fully testable.

Learns correction factors by comparing PV forecast vs actual production.
Per-month correction to handle seasonal forecast bias.
Per-weather-type correction (clear/cloudy) when data available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# EMA alpha for correction factor learning
PV_EMA_ALPHA = 0.1
MIN_SAMPLES_FOR_CORRECTION = 7  # 1 week of data


@dataclass
class PVCorrectionProfile:
    """PV forecast correction factors.

    Tracks forecast vs actual per month to learn systematic bias.
    Factor > 1.0 = forecast underestimates (actual higher).
    Factor < 1.0 = forecast overestimates (actual lower).
    """

    # Per-month correction factor (1-12)
    monthly_factor: dict[int, float] = field(default_factory=lambda: {m: 1.0 for m in range(1, 13)})

    # Per-month sample count
    monthly_samples: dict[int, int] = field(default_factory=lambda: {m: 0 for m in range(1, 13)})

    # Per-hour correction (0-23) — for intraday bias
    hourly_factor: list[float] = field(default_factory=lambda: [1.0] * 24)
    hourly_samples: list[int] = field(default_factory=lambda: [0] * 24)

    # Rolling daily records for trend analysis
    daily_records: list[dict[str, Any]] = field(default_factory=list)

    def record_daily(
        self,
        month: int,
        forecast_kwh: float,
        actual_kwh: float,
        date_str: str = "",
    ) -> None:
        """Record one day's forecast vs actual PV production.

        Args:
            month: Month (1-12).
            forecast_kwh: Forecasted daily PV production (kWh).
            actual_kwh: Actual daily PV production (kWh).
            date_str: ISO date string for trend tracking.
        """
        if forecast_kwh < 0.5:
            return  # Skip cloudy days with negligible forecast
        if actual_kwh < 0:
            return

        ratio = actual_kwh / forecast_kwh
        # Clamp to reasonable range (0.2x - 3.0x)
        ratio = max(0.2, min(3.0, ratio))

        if month < 1 or month > 12:
            return

        old = self.monthly_factor[month]
        self.monthly_factor[month] = PV_EMA_ALPHA * ratio + (1 - PV_EMA_ALPHA) * old
        self.monthly_samples[month] += 1

        # Track daily for trend analysis
        if date_str:
            self.daily_records.append(
                {
                    "date": date_str,
                    "forecast": round(forecast_kwh, 1),
                    "actual": round(actual_kwh, 1),
                    "ratio": round(ratio, 3),
                }
            )
            # Keep last 90 days
            if len(self.daily_records) > 90:
                self.daily_records = self.daily_records[-90:]

    def record_hourly(
        self,
        hour: int,
        forecast_kw: float,
        actual_kw: float,
    ) -> None:
        """Record one hour's forecast vs actual PV production.

        Args:
            hour: Hour of day (0-23).
            forecast_kw: Forecasted PV power (kW).
            actual_kw: Actual PV power (kW).
        """
        if hour < 0 or hour > 23:
            return
        if forecast_kw < 0.1:
            return  # No meaningful forecast
        if actual_kw < 0:
            return

        ratio = actual_kw / forecast_kw
        ratio = max(0.2, min(3.0, ratio))

        old = self.hourly_factor[hour]
        self.hourly_factor[hour] = PV_EMA_ALPHA * ratio + (1 - PV_EMA_ALPHA) * old
        self.hourly_samples[hour] += 1

    def correct_daily(self, month: int, forecast_kwh: float) -> float:
        """Apply monthly correction to a daily PV forecast.

        Returns corrected forecast. Returns original if insufficient data.
        """
        if month < 1 or month > 12:
            return forecast_kwh
        if self.monthly_samples.get(month, 0) < MIN_SAMPLES_FOR_CORRECTION:
            return forecast_kwh
        factor = self.monthly_factor[month]
        return round(forecast_kwh * factor, 1)

    def correct_hourly(self, hour: int, forecast_kw: float) -> float:
        """Apply hourly correction to a PV forecast.

        Returns corrected forecast. Returns original if insufficient data.
        """
        if hour < 0 or hour > 23:
            return forecast_kw
        if self.hourly_samples[hour] < MIN_SAMPLES_FOR_CORRECTION * 3:
            return forecast_kw
        factor = self.hourly_factor[hour]
        return round(forecast_kw * factor, 2)

    def correct_profile(
        self,
        month: int,
        hourly_forecast: list[float],
        start_hour: int = 0,
    ) -> list[float]:
        """Apply both monthly and hourly corrections to a 24h PV profile.

        Args:
            month: Current month (1-12).
            hourly_forecast: 24h PV forecast (kW per hour).
            start_hour: Start hour for the profile.

        Returns:
            Corrected 24h forecast.
        """
        result = []
        monthly = self.monthly_factor.get(month, 1.0)
        has_monthly = self.monthly_samples.get(month, 0) >= MIN_SAMPLES_FOR_CORRECTION

        for i, fcast in enumerate(hourly_forecast):
            h = (start_hour + i) % 24

            # Apply hourly correction if available, else monthly
            has_hourly = self.hourly_samples[h] >= MIN_SAMPLES_FOR_CORRECTION * 3
            if has_hourly:
                corrected = fcast * self.hourly_factor[h]
            elif has_monthly:
                corrected = fcast * monthly
            else:
                corrected = fcast

            result.append(round(max(0, corrected), 2))
        return result

    @property
    def overall_accuracy(self) -> float:
        """Overall forecast accuracy (0-100%).

        Based on recent daily records. 100% = perfect prediction.
        """
        if not self.daily_records:
            return 0.0
        recent = self.daily_records[-30:]
        errors = []
        for rec in recent:
            if rec["forecast"] > 0.5:
                error = abs(rec["actual"] - rec["forecast"]) / rec["forecast"]
                errors.append(error)
        if not errors:
            return 0.0
        mean_error = sum(errors) / len(errors)
        return float(round(max(0, (1 - mean_error)) * 100, 1))

    @property
    def trend(self) -> str:
        """Is forecast accuracy improving, stable, or declining?

        Compares last 7 days vs previous 7 days.
        """
        if len(self.daily_records) < 14:
            return "insufficient_data"

        recent = self.daily_records[-7:]
        previous = self.daily_records[-14:-7]

        def avg_error(records: list[dict[str, Any]]) -> float:
            errors = [abs(r["ratio"] - 1.0) for r in records if r["forecast"] > 0.5]
            return sum(errors) / len(errors) if errors else 0.5

        recent_err = avg_error(recent)
        prev_err = avg_error(previous)

        if recent_err < prev_err * 0.9:
            return "improving"
        if recent_err > prev_err * 1.1:
            return "declining"
        return "stable"

    def summary(self) -> dict[str, Any]:
        """Summary for diagnostics."""
        return {
            "overall_accuracy_pct": self.overall_accuracy,
            "trend": self.trend,
            "monthly_factors": {
                m: round(f, 3)
                for m, f in self.monthly_factor.items()
                if self.monthly_samples.get(m, 0) >= MIN_SAMPLES_FOR_CORRECTION
            },
            "total_daily_records": len(self.daily_records),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "monthly_factor": {str(k): v for k, v in self.monthly_factor.items()},
            "monthly_samples": {str(k): v for k, v in self.monthly_samples.items()},
            "hourly_factor": self.hourly_factor,
            "hourly_samples": self.hourly_samples,
            "daily_records": self.daily_records[-90:],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PVCorrectionProfile:
        """Deserialize from storage."""
        p = cls()
        mf = data.get("monthly_factor")
        if isinstance(mf, dict):
            p.monthly_factor = {int(k): float(v) for k, v in mf.items()}
        ms = data.get("monthly_samples")
        if isinstance(ms, dict):
            p.monthly_samples = {int(k): int(v) for k, v in ms.items()}
        hf = data.get("hourly_factor")
        if isinstance(hf, list) and len(hf) == 24:
            p.hourly_factor = [float(v) for v in hf]
        hs = data.get("hourly_samples")
        if isinstance(hs, list) and len(hs) == 24:
            p.hourly_samples = [int(v) for v in hs]
        dr = data.get("daily_records")
        if isinstance(dr, list):
            p.daily_records = dr[-90:]
        return p
