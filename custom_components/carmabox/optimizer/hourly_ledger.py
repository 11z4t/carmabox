"""CARMA Box — Hourly Energy Ledger.

Tracks ACTUAL energy flows per hour for transparent cost accounting.
No estimations — only measured values.

Per hour records:
- Grid import (kWh) × price (öre) = grid cost (kr)
- Battery discharge (kWh) × price = avoided cost (kr) — NEGATIVE
- Battery charge from PV (kWh) = free — no cost
- Battery charge from grid (kWh) × price = battery charge cost (kr) — POSITIVE
- EV charge from grid (kWh) × price = EV cost (kr)
- EV charge from PV (kWh) = free

Daily/weekly/monthly totals derived by summing hourly entries.

Pure Python. No HA imports. Fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HourEntry:
    """One hour of energy accounting."""

    hour: int = 0
    date: str = ""  # YYYY-MM-DD

    # Grid
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    price_ore: float = 0.0

    # Battery
    battery_discharge_kwh: float = 0.0  # Energy OUT of battery (saves grid cost)
    battery_charge_pv_kwh: float = 0.0  # Charged from solar (free)
    battery_charge_grid_kwh: float = 0.0  # Charged from grid (costs money)

    # EV
    ev_charge_grid_kwh: float = 0.0  # EV from grid (costs money)
    ev_charge_pv_kwh: float = 0.0  # EV from solar (free)

    # Appliances — kWh per category this hour
    appliance_kwh: dict[str, float] = field(default_factory=dict)

    # Ellevio
    weighted_avg_kw: float = 0.0  # Hourly weighted average (Ellevio definition)

    # CARMA-LEDGER-FIELDS: New tracking fields
    solar_kwh: float = 0.0  # Total PV production this hour
    export_kwh: float = 0.0  # Grid export this hour (alias for grid_export_kwh)
    house_consumption_kwh: float = 0.0  # Total house consumption
    battery_soc_pct: float = 0.0  # Battery SoC at hour end (avg of both inverters)
    ev_soc_pct: float = 0.0  # EV SoC at hour end
    miner_kwh: float = 0.0  # Miner consumption this hour
    action: str = ""  # CARMA decision this hour (idle/discharge/grid_charge/etc)
    temperature_c: float = 0.0  # Outdoor temperature
    cell_temp_min_c: float | None = None  # IT-1948: Min battery cell temp this hour

    @property
    def grid_cost_kr(self) -> float:
        """What grid import cost this hour (kr)."""
        return round(self.grid_import_kwh * self.price_ore / 100, 2)

    @property
    def battery_saved_kr(self) -> float:
        """What battery discharge saved us (kr) — negative = saved."""
        return round(-self.battery_discharge_kwh * self.price_ore / 100, 2)

    @property
    def battery_charge_cost_kr(self) -> float:
        """What grid-charging the battery cost (kr)."""
        return round(self.battery_charge_grid_kwh * self.price_ore / 100, 2)

    @property
    def ev_cost_kr(self) -> float:
        """What EV grid charging cost (kr). PV charging = 0."""
        return round(self.ev_charge_grid_kwh * self.price_ore / 100, 2)

    @property
    def total_cost_kr(self) -> float:
        """Total cost this hour (grid + battery charge + EV - battery savings)."""
        return round(
            self.grid_cost_kr
            + self.battery_charge_cost_kr
            + self.ev_cost_kr
            + self.battery_saved_kr,
            2,
        )

    @property
    def cost_without_battery_kr(self) -> float:
        """What this hour would have cost WITHOUT battery (counterfactual)."""
        # Without battery: all discharge would be grid import instead
        return round(
            (self.grid_import_kwh + self.battery_discharge_kwh) * self.price_ore / 100,
            2,
        )

    @property
    def appliance_cost_kr(self) -> dict[str, float]:
        """Cost per appliance category this hour (kr)."""
        return {
            cat: round(kwh * self.price_ore / 100, 2)
            for cat, kwh in self.appliance_kwh.items()
            if kwh > 0.001
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for sensor attributes / mail table."""
        result: dict[str, Any] = {
            "hour": self.hour,
            "date": self.date,
            "price_ore": round(self.price_ore, 1),
            "grid_kwh": round(self.grid_import_kwh, 2),
            "grid_export_kwh": round(self.grid_export_kwh, 2),
            "grid_cost_kr": self.grid_cost_kr,
            "bat_discharge_kwh": round(self.battery_discharge_kwh, 2),
            "bat_saved_kr": self.battery_saved_kr,
            "bat_charge_pv_kwh": round(self.battery_charge_pv_kwh, 2),
            "bat_charge_grid_kwh": round(self.battery_charge_grid_kwh, 2),
            "bat_charge_cost_kr": self.battery_charge_cost_kr,
            "ev_grid_kwh": round(self.ev_charge_grid_kwh, 2),
            "ev_cost_kr": self.ev_cost_kr,
            "total_cost_kr": self.total_cost_kr,
            "without_battery_kr": self.cost_without_battery_kr,
            "weighted_kw": round(self.weighted_avg_kw, 2),
            # CARMA-LEDGER-FIELDS: New fields
            "solar_kwh": round(self.solar_kwh, 2),
            "export_kwh": round(self.export_kwh, 2),
            "house_consumption_kwh": round(self.house_consumption_kwh, 2),
            "battery_soc_pct": round(self.battery_soc_pct, 1),
            "ev_soc_pct": round(self.ev_soc_pct, 1),
            "miner_kwh": round(self.miner_kwh, 3),
            "action": self.action,
            "temperature_c": round(self.temperature_c, 1),
            "cell_temp_min_c": (
                round(self.cell_temp_min_c, 1) if self.cell_temp_min_c is not None else None
            ),
        }
        if self.appliance_kwh:
            result["appliances"] = {
                cat: {"kwh": round(kwh, 3), "cost_kr": round(kwh * self.price_ore / 100, 2)}
                for cat, kwh in self.appliance_kwh.items()
                if kwh > 0.001
            }
        return result


@dataclass
class EnergyLedger:
    """Rolling ledger of hourly energy entries.

    Keeps 24h (today), 7 days, 30 days of history.
    """

    entries: list[HourEntry] = field(default_factory=list)
    _current_hour: int = -1
    _current_date: str = ""

    # Accumulator for current hour (30s samples → hourly totals)
    _acc_grid_import_w: float = 0.0
    _acc_grid_export_w: float = 0.0
    _acc_bat_discharge_w: float = 0.0
    _acc_bat_charge_w: float = 0.0
    _acc_ev_w: float = 0.0
    _acc_pv_w: float = 0.0
    _acc_price_ore: float = 0.0
    _acc_weighted_kw_sum: float = 0.0
    _acc_samples: int = 0
    _acc_appliances: dict[str, float] = field(default_factory=dict)

    # CARMA-LEDGER-FIELDS: New accumulators
    _acc_solar_w: float = 0.0  # Total solar production accumulator
    _acc_export_w: float = 0.0  # Export accumulator (same as _acc_grid_export_w)
    _acc_house_w: float = 0.0  # House consumption accumulator
    _acc_miner_w: float = 0.0  # Miner consumption accumulator
    _last_battery_soc: float = 0.0  # Snapshot at hour end
    _last_ev_soc: float = 0.0  # Snapshot at hour end
    _last_action: str = ""  # Snapshot at hour end
    _last_temp: float = 0.0  # Snapshot at hour end
    _last_cell_temp_min: float | None = None  # IT-1948: Min cell temp snapshot

    def record_sample(
        self,
        hour: int,
        date_str: str,
        grid_w: float,
        battery_w: float,
        pv_w: float,
        ev_w: float,
        price_ore: float,
        weighted_kw: float,
        is_exporting: bool,
        interval_s: float = 30.0,
        appliance_power: dict[str, float] | None = None,
        solar_w: float = 0.0,
        house_w: float = 0.0,
        miner_w: float = 0.0,
        battery_soc: float = 0.0,
        ev_soc: float = 0.0,
        action: str = "",
        temperature_c: float = 0.0,
        cell_temp_min_c: float | None = None,
    ) -> None:
        """Record a 30-second sample.

        Called every scan cycle. Accumulates into hourly entry.
        At hour boundary, flushes accumulated data to a new HourEntry.
        """
        # Hour changed — flush previous hour
        if hour != self._current_hour and self._current_hour >= 0:
            self._flush_hour()

        self._current_hour = hour
        self._current_date = date_str

        # Convert W × seconds → Wh → kWh
        wh_factor = interval_s / 3600  # 30s = 0.00833h

        # Grid
        if grid_w > 0:
            self._acc_grid_import_w += grid_w * wh_factor
        else:
            self._acc_grid_export_w += abs(grid_w) * wh_factor

        # Battery (positive = charging, negative = discharging)
        if battery_w < 0:
            # Discharging
            self._acc_bat_discharge_w += abs(battery_w) * wh_factor
        elif battery_w > 0:
            # Charging — from PV or grid?
            if is_exporting or pv_w > battery_w:
                # PV covers the charge
                self._acc_bat_charge_w += 0  # PV charge tracked separately
                self._acc_pv_w += battery_w * wh_factor
            else:
                # Grid is contributing to charge
                grid_portion = max(0, battery_w - max(0, pv_w))
                pv_portion = battery_w - grid_portion
                self._acc_bat_charge_w += grid_portion * wh_factor
                self._acc_pv_w += pv_portion * wh_factor

        # EV
        if ev_w > 0:
            if is_exporting:
                pass  # PV-powered EV — free
            else:
                self._acc_ev_w += ev_w * wh_factor

        # Appliances (W → Wh per category)
        if appliance_power:
            for cat, w in appliance_power.items():
                if w > 0:
                    self._acc_appliances[cat] = self._acc_appliances.get(cat, 0.0) + w * wh_factor

        # Price + weighted average
        self._acc_price_ore += price_ore
        self._acc_weighted_kw_sum += weighted_kw
        self._acc_samples += 1

        # CARMA-LEDGER-FIELDS: Accumulate new fields
        self._acc_solar_w += solar_w * wh_factor
        self._acc_house_w += house_w * wh_factor
        self._acc_miner_w += miner_w * wh_factor

        # Snapshot values at hour end (overwrite each sample)
        self._last_battery_soc = battery_soc
        self._last_ev_soc = ev_soc
        self._last_action = action
        self._last_temp = temperature_c
        # IT-1948: Track min cell temp (keep the lowest seen this hour)
        if cell_temp_min_c is not None and (
            self._last_cell_temp_min is None or cell_temp_min_c < self._last_cell_temp_min
        ):
            self._last_cell_temp_min = cell_temp_min_c

    def _flush_hour(self) -> None:
        """Flush accumulated samples into an HourEntry."""
        if self._acc_samples == 0:
            return

        avg_price = self._acc_price_ore / self._acc_samples
        avg_weighted = self._acc_weighted_kw_sum / self._acc_samples

        # Convert appliance Wh → kWh
        app_kwh = {cat: wh / 1000 for cat, wh in self._acc_appliances.items() if wh > 0}

        entry = HourEntry(
            hour=self._current_hour,
            date=self._current_date,
            grid_import_kwh=self._acc_grid_import_w / 1000,
            grid_export_kwh=self._acc_grid_export_w / 1000,
            price_ore=avg_price,
            battery_discharge_kwh=self._acc_bat_discharge_w / 1000,
            battery_charge_pv_kwh=self._acc_pv_w / 1000,
            battery_charge_grid_kwh=self._acc_bat_charge_w / 1000,
            ev_charge_grid_kwh=self._acc_ev_w / 1000,
            weighted_avg_kw=avg_weighted,
            appliance_kwh=app_kwh,
            # CARMA-LEDGER-FIELDS: New fields
            solar_kwh=self._acc_solar_w / 1000,
            export_kwh=self._acc_grid_export_w / 1000,
            house_consumption_kwh=self._acc_house_w / 1000,
            battery_soc_pct=self._last_battery_soc,
            ev_soc_pct=self._last_ev_soc,
            miner_kwh=self._acc_miner_w / 1000,
            action=self._last_action,
            temperature_c=self._last_temp,
            cell_temp_min_c=self._last_cell_temp_min,
        )
        self.entries.append(entry)

        # Keep max 30 days (720 hours)
        if len(self.entries) > 720:
            self.entries = self.entries[-720:]

        # Reset accumulators
        self._acc_grid_import_w = 0.0
        self._acc_grid_export_w = 0.0
        self._acc_bat_discharge_w = 0.0
        self._acc_bat_charge_w = 0.0
        self._acc_ev_w = 0.0
        self._acc_pv_w = 0.0
        self._acc_price_ore = 0.0
        self._acc_weighted_kw_sum = 0.0
        self._acc_samples = 0
        self._acc_appliances = {}

        # CARMA-LEDGER-FIELDS: Reset new accumulators
        self._acc_solar_w = 0.0
        self._acc_house_w = 0.0
        self._acc_miner_w = 0.0
        self._last_battery_soc = 0.0
        self._last_ev_soc = 0.0
        self._last_action = ""
        self._last_temp = 0.0
        self._last_cell_temp_min = None

    def today(self, date_str: str) -> list[HourEntry]:
        """Get today's entries."""
        return [e for e in self.entries if e.date == date_str]

    def last_24h(self) -> list[HourEntry]:
        """Get last 24 hours of entries."""
        return self.entries[-24:] if len(self.entries) >= 24 else list(self.entries)

    def daily_summary(self, date_str: str) -> dict[str, Any]:
        """Summarize a single day."""
        day = self.today(date_str)
        if not day:
            return {"status": "no_data", "date": date_str}

        total_grid = sum(e.grid_cost_kr for e in day)
        total_saved = sum(e.battery_saved_kr for e in day)
        total_bat_charge = sum(e.battery_charge_cost_kr for e in day)
        total_ev = sum(e.ev_cost_kr for e in day)
        total_cost = sum(e.total_cost_kr for e in day)
        total_without = sum(e.cost_without_battery_kr for e in day)
        battery_net_saving = round(total_without - total_cost, 2)

        prices = [e.price_ore for e in day if e.price_ore > 0]
        weighted_avgs = [e.weighted_avg_kw for e in day if e.weighted_avg_kw > 0]

        # CARMA-LEDGER-FIELDS: Aggregate new fields
        total_solar = sum(e.solar_kwh for e in day)
        total_export = sum(e.export_kwh for e in day)
        total_house = sum(e.house_consumption_kwh for e in day)
        total_miner = sum(e.miner_kwh for e in day)
        temps = [e.temperature_c for e in day if e.temperature_c != 0]

        cheapest = min(day, key=lambda e: e.total_cost_kr) if day else None
        most_expensive = max(day, key=lambda e: e.total_cost_kr) if day else None
        best_hour = min(day, key=lambda e: e.weighted_avg_kw) if day else None
        worst_hour = max(day, key=lambda e: e.weighted_avg_kw) if day else None

        return {
            "date": date_str,
            "hours": len(day),
            "grid_cost_kr": round(total_grid, 2),
            "battery_saved_kr": round(abs(total_saved), 2),
            "battery_charge_cost_kr": round(total_bat_charge, 2),
            "ev_cost_kr": round(total_ev, 2),
            "total_cost_kr": round(total_cost, 2),
            "without_battery_kr": round(total_without, 2),
            "battery_net_saving_kr": battery_net_saving,
            # Price stats
            "avg_price_ore": round(sum(prices) / len(prices), 1) if prices else 0,
            "min_price_ore": round(min(prices), 1) if prices else 0,
            "max_price_ore": round(max(prices), 1) if prices else 0,
            # Ellevio stats
            "avg_weighted_kw": (
                round(sum(weighted_avgs) / len(weighted_avgs), 2) if weighted_avgs else 0
            ),
            "min_weighted_kw": round(min(weighted_avgs), 2) if weighted_avgs else 0,
            "max_weighted_kw": round(max(weighted_avgs), 2) if weighted_avgs else 0,
            # Extremes
            "cheapest_hour": cheapest.hour if cheapest else -1,
            "cheapest_cost_kr": cheapest.total_cost_kr if cheapest else 0,
            "most_expensive_hour": most_expensive.hour if most_expensive else -1,
            "most_expensive_cost_kr": (most_expensive.total_cost_kr if most_expensive else 0),
            "best_ellevio_hour": best_hour.hour if best_hour else -1,
            "best_ellevio_kw": best_hour.weighted_avg_kw if best_hour else 0,
            "worst_ellevio_hour": worst_hour.hour if worst_hour else -1,
            "worst_ellevio_kw": worst_hour.weighted_avg_kw if worst_hour else 0,
            # CARMA-LEDGER-FIELDS: New aggregates
            "total_solar_kwh": round(total_solar, 2),
            "total_export_kwh": round(total_export, 2),
            "total_house_kwh": round(total_house, 2),
            "total_miner_kwh": round(total_miner, 3),
            "min_temp_c": round(min(temps), 1) if temps else 0,
            "max_temp_c": round(max(temps), 1) if temps else 0,
            "avg_temp_c": round(sum(temps) / len(temps), 1) if temps else 0,
            # Hourly table for mail
            "hourly": [e.to_dict() for e in day],
        }

    def period_summary(self, days: int) -> dict[str, Any]:
        """Summarize last N days."""
        if not self.entries:
            return {"status": "no_data", "days": 0}

        dates = sorted(set(e.date for e in self.entries))
        recent = dates[-days:] if len(dates) >= days else dates

        total_cost = 0.0
        total_without = 0.0
        total_ev = 0.0
        for date in recent:
            s = self.daily_summary(date)
            total_cost += s.get("total_cost_kr", 0)
            total_without += s.get("without_battery_kr", 0)
            total_ev += s.get("ev_cost_kr", 0)

        return {
            "days": len(recent),
            "total_cost_kr": round(total_cost, 2),
            "without_battery_kr": round(total_without, 2),
            "battery_saving_kr": round(total_without - total_cost, 2),
            "ev_cost_kr": round(total_ev, 2),
            "avg_daily_cost_kr": (round(total_cost / len(recent), 2) if recent else 0),
            "avg_daily_saving_kr": (
                round((total_without - total_cost) / len(recent), 2) if recent else 0
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistent storage."""
        return {
            "entries": [e.to_dict() for e in self.entries[-720:]],
            "current_hour": self._current_hour,
            "current_date": self._current_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnergyLedger:
        """Deserialize from storage."""
        ledger = cls()
        for ed in data.get("entries", []):
            ledger.entries.append(
                HourEntry(
                    hour=ed.get("hour", 0),
                    date=ed.get("date", ""),
                    grid_import_kwh=ed.get("grid_kwh", 0),
                    grid_export_kwh=ed.get("grid_export_kwh", 0),
                    price_ore=ed.get("price_ore", 0),
                    battery_discharge_kwh=ed.get("bat_discharge_kwh", 0),
                    battery_charge_pv_kwh=ed.get("bat_charge_pv_kwh", 0),
                    battery_charge_grid_kwh=ed.get("bat_charge_grid_kwh", 0),
                    ev_charge_grid_kwh=ed.get("ev_grid_kwh", 0),
                    weighted_avg_kw=ed.get("weighted_kw", 0),
                    # CARMA-LEDGER-FIELDS: Deserialize new fields
                    solar_kwh=ed.get("solar_kwh", 0),
                    export_kwh=ed.get("export_kwh", 0),
                    house_consumption_kwh=ed.get("house_consumption_kwh", 0),
                    battery_soc_pct=ed.get("battery_soc_pct", 0),
                    ev_soc_pct=ed.get("ev_soc_pct", 0),
                    miner_kwh=ed.get("miner_kwh", 0),
                    action=ed.get("action", ""),
                    temperature_c=ed.get("temperature_c", 0),
                    cell_temp_min_c=ed.get("cell_temp_min_c"),
                )
            )
        ledger._current_hour = data.get("current_hour", -1)
        ledger._current_date = data.get("current_date", "")
        return ledger
