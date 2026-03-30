"""Tests for HourEntry and EnergyLedger."""

from __future__ import annotations

import pytest

from custom_components.carmabox.optimizer.hourly_ledger import (
    EnergyLedger,
    HourEntry,
)


def _make_entry(**kwargs) -> HourEntry:
    """Helper to create a HourEntry with defaults."""
    defaults = {"hour": 12, "date": "2026-03-30", "price_ore": 100.0}
    defaults.update(kwargs)
    return HourEntry(**defaults)


def _record_and_flush(ledger: EnergyLedger, hour: int, **kwargs) -> None:
    """Record a sample then trigger flush by advancing hour."""
    ledger.record_sample(
        hour=hour,
        date_str="2026-03-30",
        grid_w=kwargs.get("grid_w", 0.0),
        battery_w=kwargs.get("battery_w", 0.0),
        pv_w=kwargs.get("pv_w", 0.0),
        ev_w=kwargs.get("ev_w", 0.0),
        price_ore=kwargs.get("price_ore", 80.0),
        weighted_kw=kwargs.get("weighted_kw", 0.0),
        is_exporting=kwargs.get("is_exporting", False),
        interval_s=kwargs.get("interval_s", 30.0),
        **{
            k: v
            for k, v in kwargs.items()
            if k
            not in (
                "grid_w",
                "battery_w",
                "pv_w",
                "ev_w",
                "price_ore",
                "weighted_kw",
                "is_exporting",
                "interval_s",
            )
        },
    )
    # Flush by advancing to next hour
    ledger.record_sample(
        hour=hour + 1,
        date_str="2026-03-30",
        grid_w=0.0,
        battery_w=0.0,
        pv_w=0.0,
        ev_w=0.0,
        price_ore=80.0,
        weighted_kw=0.0,
        is_exporting=False,
    )


class TestHourEntryProperties:
    def test_grid_cost_kr(self) -> None:
        e = _make_entry(grid_import_kwh=1.0, price_ore=100.0)
        assert e.grid_cost_kr == pytest.approx(1.00)

    def test_grid_cost_zero_when_no_import(self) -> None:
        e = _make_entry(grid_import_kwh=0.0, price_ore=100.0)
        assert e.grid_cost_kr == 0.0

    def test_battery_saved_kr_is_negative(self) -> None:
        e = _make_entry(battery_discharge_kwh=1.0, price_ore=100.0)
        assert e.battery_saved_kr == pytest.approx(-1.00)

    def test_battery_saved_kr_zero_when_no_discharge(self) -> None:
        e = _make_entry(battery_discharge_kwh=0.0, price_ore=100.0)
        assert e.battery_saved_kr == 0.0

    def test_battery_charge_cost_kr(self) -> None:
        e = _make_entry(battery_charge_grid_kwh=2.0, price_ore=50.0)
        assert e.battery_charge_cost_kr == pytest.approx(1.00)

    def test_ev_cost_kr(self) -> None:
        e = _make_entry(ev_charge_grid_kwh=3.0, price_ore=80.0)
        assert e.ev_cost_kr == pytest.approx(2.40)

    def test_ev_pv_charge_no_cost(self) -> None:
        e = _make_entry(ev_charge_pv_kwh=5.0, price_ore=100.0)
        assert e.ev_cost_kr == 0.0  # PV charging is free

    def test_total_cost_grid_minus_battery(self) -> None:
        e = _make_entry(
            grid_import_kwh=2.0,
            battery_discharge_kwh=1.0,
            battery_charge_grid_kwh=0.0,
            ev_charge_grid_kwh=0.0,
            price_ore=100.0,
        )
        # grid=2.0, bat_saved=-1.0 → total=1.0
        assert e.total_cost_kr == pytest.approx(1.00)

    def test_total_cost_includes_ev(self) -> None:
        e = _make_entry(
            grid_import_kwh=1.0,
            ev_charge_grid_kwh=1.0,
            price_ore=100.0,
        )
        assert e.total_cost_kr == pytest.approx(2.00)

    def test_cost_without_battery(self) -> None:
        e = _make_entry(grid_import_kwh=1.0, battery_discharge_kwh=1.0, price_ore=100.0)
        assert e.cost_without_battery_kr == pytest.approx(2.00)

    def test_cost_without_battery_no_discharge(self) -> None:
        e = _make_entry(grid_import_kwh=2.0, battery_discharge_kwh=0.0, price_ore=100.0)
        assert e.cost_without_battery_kr == pytest.approx(2.00)

    def test_appliance_cost_kr(self) -> None:
        e = _make_entry(appliance_kwh={"laundry": 2.0, "heating": 1.0}, price_ore=100.0)
        costs = e.appliance_cost_kr
        assert costs["laundry"] == pytest.approx(2.00)
        assert costs["heating"] == pytest.approx(1.00)

    def test_appliance_cost_skips_tiny_kwh(self) -> None:
        e = _make_entry(appliance_kwh={"laundry": 0.0005}, price_ore=100.0)
        assert "laundry" not in e.appliance_cost_kr

    def test_appliance_cost_empty(self) -> None:
        e = _make_entry(appliance_kwh={})
        assert e.appliance_cost_kr == {}


class TestHourEntryToDict:
    def test_required_keys_present(self) -> None:
        e = _make_entry(hour=14, date="2026-03-30", price_ore=80.0)
        d = e.to_dict()
        for key in (
            "hour",
            "date",
            "price_ore",
            "grid_kwh",
            "bat_discharge_kwh",
            "total_cost_kr",
            "solar_kwh",
            "tvatt_kwh",
            "tork_kwh",
            "disk_kwh",
            "action",
            "temperature_c",
        ):
            assert key in d, f"Missing key: {key}"

    def test_hour_and_date(self) -> None:
        e = _make_entry(hour=14, date="2026-03-30")
        d = e.to_dict()
        assert d["hour"] == 14
        assert d["date"] == "2026-03-30"

    def test_appliances_section_only_when_nonempty(self) -> None:
        e = _make_entry(appliance_kwh={})
        d = e.to_dict()
        assert "appliances" not in d

    def test_appliances_section_included_when_nonempty(self) -> None:
        e = _make_entry(appliance_kwh={"laundry": 1.5}, price_ore=100.0)
        d = e.to_dict()
        assert "appliances" in d
        assert "laundry" in d["appliances"]


class TestEnergyLedgerInit:
    def test_initial_state(self) -> None:
        ledger = EnergyLedger()
        assert ledger.entries == []
        assert ledger._current_hour == -1
        assert ledger._current_date == ""


class TestRecordSample:
    def test_accumulates_within_hour(self) -> None:
        ledger = EnergyLedger()
        ledger.record_sample(
            hour=10,
            date_str="2026-03-30",
            grid_w=1000.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=80.0,
            weighted_kw=1.0,
            is_exporting=False,
        )
        assert ledger._acc_grid_import_w > 0
        assert len(ledger.entries) == 0

    def test_flush_on_hour_change(self) -> None:
        ledger = EnergyLedger()
        ledger.record_sample(
            hour=10,
            date_str="2026-03-30",
            grid_w=1000.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=80.0,
            weighted_kw=1.0,
            is_exporting=False,
        )
        ledger.record_sample(
            hour=11,
            date_str="2026-03-30",
            grid_w=500.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=90.0,
            weighted_kw=0.5,
            is_exporting=False,
        )
        assert len(ledger.entries) == 1
        assert ledger.entries[0].hour == 10

    def test_correct_kwh_from_watt_seconds(self) -> None:
        ledger = EnergyLedger()
        # 1000W x 30s = 30,000 Ws = 8.333 Wh = 0.00833 kWh
        ledger.record_sample(
            hour=5,
            date_str="2026-03-30",
            grid_w=1000.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=80.0,
            weighted_kw=1.0,
            is_exporting=False,
            interval_s=30.0,
        )
        ledger.record_sample(
            hour=6,
            date_str="2026-03-30",
            grid_w=0.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=80.0,
            weighted_kw=0.0,
            is_exporting=False,
        )
        expected = 1000.0 * 30 / 3600 / 1000
        assert ledger.entries[0].grid_import_kwh == pytest.approx(expected, rel=1e-3)

    def test_battery_discharge_tracked(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 7, battery_w=-2000.0, price_ore=100.0, weighted_kw=2.0)
        assert ledger.entries[0].battery_discharge_kwh > 0

    def test_grid_export_tracked(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 12, grid_w=-500.0, pv_w=1000.0, is_exporting=True)
        assert ledger.entries[0].grid_export_kwh > 0

    def test_ev_grid_charge_tracked(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 8, ev_w=7400.0, is_exporting=False)
        assert ledger.entries[0].ev_charge_grid_kwh > 0

    def test_ev_pv_charge_not_counted_as_grid(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 13, ev_w=3000.0, is_exporting=True)
        assert ledger.entries[0].ev_charge_grid_kwh == 0.0

    def test_battery_pv_charge_tracked(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 11, battery_w=2000.0, pv_w=5000.0, is_exporting=True)
        entry = ledger.entries[0]
        assert entry.battery_charge_grid_kwh == 0.0  # PV covers it
        assert entry.battery_charge_pv_kwh >= 0.0

    def test_battery_grid_charge_when_pv_insufficient(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 22, battery_w=3000.0, pv_w=0.0, is_exporting=False)
        assert ledger.entries[0].battery_charge_grid_kwh > 0

    def test_price_averaged_over_samples(self) -> None:
        ledger = EnergyLedger()
        for _ in range(3):
            ledger.record_sample(
                hour=9,
                date_str="2026-03-30",
                grid_w=100.0,
                battery_w=0.0,
                pv_w=0.0,
                ev_w=0.0,
                price_ore=90.0,
                weighted_kw=0.1,
                is_exporting=False,
            )
        ledger.record_sample(
            hour=10,
            date_str="2026-03-30",
            grid_w=0.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=90.0,
            weighted_kw=0.0,
            is_exporting=False,
        )
        assert ledger.entries[0].price_ore == pytest.approx(90.0)

    def test_solar_accumulated(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 14, solar_w=3000.0)
        assert ledger.entries[0].solar_kwh > 0

    def test_house_consumption_accumulated(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 15, house_w=2000.0)
        assert ledger.entries[0].house_consumption_kwh > 0

    def test_miner_accumulated(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 16, miner_w=1500.0)
        assert ledger.entries[0].miner_kwh > 0

    def test_snapshot_fields_at_hour_end(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(
            ledger,
            8,
            battery_soc=85.0,
            ev_soc=50.0,
            action="idle",
            temperature_c=10.0,
        )
        entry = ledger.entries[0]
        assert entry.battery_soc_pct == 85.0
        assert entry.ev_soc_pct == 50.0
        assert entry.action == "idle"
        assert entry.temperature_c == 10.0

    def test_per_appliance_tracked(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 14, tvatt_w=1000.0, disk_w=500.0, tork_w=200.0)
        entry = ledger.entries[0]
        assert entry.tvatt_kwh > 0
        assert entry.disk_kwh > 0
        assert entry.tork_kwh > 0

    def test_cell_temp_snapshot(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(
            ledger,
            6,
            cell_temp_kontor_c=18.5,
            cell_temp_forrad_c=17.0,
        )
        entry = ledger.entries[0]
        assert entry.cell_temp_kontor_c == 18.5
        assert entry.cell_temp_forrad_c == 17.0

    def test_appliance_power_dict(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 10, appliance_power={"laundry": 1000.0, "heating": 2000.0})
        entry = ledger.entries[0]
        assert "laundry" in entry.appliance_kwh
        assert "heating" in entry.appliance_kwh

    def test_resets_accumulators_after_flush(self) -> None:
        ledger = EnergyLedger()
        _record_and_flush(ledger, 10, grid_w=1000.0)
        # After flush, grid accumulator is reset (the next sample has grid_w=0)
        assert ledger._acc_grid_import_w == 0.0
        # Samples counter was reset then incremented by the flush-triggering sample
        assert ledger._acc_samples == 1


class TestFlushEdgeCases:
    def test_no_flush_on_first_sample(self) -> None:
        ledger = EnergyLedger()
        # _current_hour starts at -1, so no flush on first sample
        ledger.record_sample(
            hour=5,
            date_str="2026-03-30",
            grid_w=100.0,
            battery_w=0.0,
            pv_w=0.0,
            ev_w=0.0,
            price_ore=80.0,
            weighted_kw=0.1,
            is_exporting=False,
        )
        assert len(ledger.entries) == 0

    def test_no_flush_if_no_samples_accumulated(self) -> None:
        """_flush_hour with 0 samples should not create entry."""
        ledger = EnergyLedger()
        ledger._current_hour = 5  # simulate we're in hour 5
        ledger._acc_samples = 0
        ledger._flush_hour()
        assert len(ledger.entries) == 0

    def test_max_720_entries_enforced(self) -> None:
        ledger = EnergyLedger()
        for i in range(750):
            ledger.entries.append(HourEntry(hour=i % 24, date="2026-03-30"))
        # Trigger a flush
        _record_and_flush(ledger, 0, grid_w=100.0)
        assert len(ledger.entries) <= 720


class TestTodayAndLast24h:
    def test_today_filters_by_date(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(_make_entry(hour=10, date="2026-03-30"))
        ledger.entries.append(_make_entry(hour=11, date="2026-03-30"))
        ledger.entries.append(_make_entry(hour=12, date="2026-03-29"))  # different date
        result = ledger.today("2026-03-30")
        assert len(result) == 2

    def test_today_empty(self) -> None:
        ledger = EnergyLedger()
        assert ledger.today("2026-03-30") == []

    def test_last_24h_returns_last_24(self) -> None:
        ledger = EnergyLedger()
        for i in range(30):
            ledger.entries.append(_make_entry(hour=i % 24))
        result = ledger.last_24h()
        assert len(result) == 24

    def test_last_24h_fewer_than_24_entries(self) -> None:
        ledger = EnergyLedger()
        for i in range(10):
            ledger.entries.append(_make_entry(hour=i))
        result = ledger.last_24h()
        assert len(result) == 10


class TestDailySummary:
    def test_no_data_returns_status(self) -> None:
        ledger = EnergyLedger()
        result = ledger.daily_summary("2026-03-30")
        assert result["status"] == "no_data"
        assert result["date"] == "2026-03-30"

    def test_summary_with_data(self) -> None:
        ledger = EnergyLedger()
        for h in range(5):
            ledger.entries.append(
                _make_entry(
                    hour=h,
                    date="2026-03-30",
                    grid_import_kwh=1.0,
                    battery_discharge_kwh=0.5,
                    price_ore=80.0,
                    weighted_avg_kw=1.0,
                )
            )
        result = ledger.daily_summary("2026-03-30")
        assert result["date"] == "2026-03-30"
        assert result["hours"] == 5
        assert result["grid_cost_kr"] > 0
        assert result["battery_saved_kr"] > 0

    def test_summary_price_stats(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(_make_entry(hour=0, date="2026-03-30", price_ore=50.0))
        ledger.entries.append(_make_entry(hour=1, date="2026-03-30", price_ore=100.0))
        result = ledger.daily_summary("2026-03-30")
        assert result["min_price_ore"] == pytest.approx(50.0)
        assert result["max_price_ore"] == pytest.approx(100.0)
        assert result["avg_price_ore"] == pytest.approx(75.0)

    def test_summary_includes_battery_net_saving(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(
            _make_entry(
                hour=12,
                date="2026-03-30",
                grid_import_kwh=1.0,
                battery_discharge_kwh=1.0,
                price_ore=100.0,
            )
        )
        result = ledger.daily_summary("2026-03-30")
        assert "battery_net_saving_kr" in result
        # without_battery = (1+1)x100/100 = 2.0; total = (1.0-1.0)x100/100 = 0.0; saving = 2.0
        assert result["battery_net_saving_kr"] == pytest.approx(2.0)

    def test_summary_includes_solar_and_appliances(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(
            _make_entry(
                hour=12,
                date="2026-03-30",
                solar_kwh=3.0,
                tvatt_kwh=1.0,
                disk_kwh=0.5,
                price_ore=80.0,
            )
        )
        result = ledger.daily_summary("2026-03-30")
        assert result["total_solar_kwh"] == pytest.approx(3.0)
        assert result["total_tvatt_kwh"] == pytest.approx(1.0)
        assert result["total_disk_kwh"] == pytest.approx(0.5)

    def test_summary_extremes(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(
            _make_entry(hour=0, date="2026-03-30", price_ore=50.0, grid_import_kwh=1.0)
        )
        ledger.entries.append(
            _make_entry(hour=1, date="2026-03-30", price_ore=200.0, grid_import_kwh=5.0)
        )
        result = ledger.daily_summary("2026-03-30")
        assert result["cheapest_hour"] == 0
        assert result["most_expensive_hour"] == 1


class TestPeriodSummary:
    def test_no_data(self) -> None:
        ledger = EnergyLedger()
        result = ledger.period_summary(7)
        assert result["status"] == "no_data"

    def test_with_data(self) -> None:
        ledger = EnergyLedger()
        for d in range(3):
            date = f"2026-03-{28 + d:02d}"
            ledger.entries.append(
                _make_entry(
                    hour=12,
                    date=date,
                    grid_import_kwh=2.0,
                    battery_discharge_kwh=1.0,
                    price_ore=100.0,
                )
            )
        result = ledger.period_summary(7)
        assert result["days"] == 3
        assert result["total_cost_kr"] > 0

    def test_period_limited_to_n_days(self) -> None:
        ledger = EnergyLedger()
        for d in range(10):
            date = f"2026-03-{1 + d:02d}"
            ledger.entries.append(_make_entry(hour=12, date=date, price_ore=100.0))
        result = ledger.period_summary(5)
        assert result["days"] == 5

    def test_battery_saving_computed(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(
            _make_entry(
                hour=12,
                date="2026-03-30",
                grid_import_kwh=1.0,
                battery_discharge_kwh=1.0,
                price_ore=100.0,
            )
        )
        result = ledger.period_summary(1)
        assert result["battery_saving_kr"] >= 0

    def test_avg_daily_cost(self) -> None:
        ledger = EnergyLedger()
        for d in range(2):
            ledger.entries.append(
                _make_entry(
                    hour=12,
                    date=f"2026-03-{29 + d:02d}",
                    grid_import_kwh=2.0,
                    price_ore=100.0,
                )
            )
        result = ledger.period_summary(7)
        assert "avg_daily_cost_kr" in result


class TestSerialization:
    def test_to_dict_keys(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(_make_entry())
        d = ledger.to_dict()
        assert "entries" in d
        assert "current_hour" in d
        assert "current_date" in d

    def test_to_dict_caps_at_720(self) -> None:
        ledger = EnergyLedger()
        for i in range(800):
            ledger.entries.append(_make_entry(hour=i % 24))
        d = ledger.to_dict()
        assert len(d["entries"]) == 720

    def test_from_dict_empty(self) -> None:
        ledger = EnergyLedger.from_dict({})
        assert ledger.entries == []
        assert ledger._current_hour == -1
        assert ledger._current_date == ""

    def test_roundtrip_single_entry(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(
            _make_entry(
                hour=10,
                date="2026-03-30",
                grid_import_kwh=2.0,
                price_ore=80.0,
                battery_discharge_kwh=1.0,
            )
        )
        d = ledger.to_dict()
        ledger2 = EnergyLedger.from_dict(d)
        assert len(ledger2.entries) == 1
        e = ledger2.entries[0]
        assert e.hour == 10
        assert e.date == "2026-03-30"
        assert e.grid_import_kwh == pytest.approx(2.0)
        assert e.battery_discharge_kwh == pytest.approx(1.0)

    def test_from_dict_restores_per_appliance(self) -> None:
        ledger = EnergyLedger()
        ledger.entries.append(_make_entry(tvatt_kwh=1.5, disk_kwh=0.8))
        d = ledger.to_dict()
        ledger2 = EnergyLedger.from_dict(d)
        assert ledger2.entries[0].tvatt_kwh == pytest.approx(1.5)
        assert ledger2.entries[0].disk_kwh == pytest.approx(0.8)

    def test_from_dict_restores_current_hour(self) -> None:
        ledger = EnergyLedger()
        ledger._current_hour = 14
        ledger._current_date = "2026-03-30"
        d = ledger.to_dict()
        ledger2 = EnergyLedger.from_dict(d)
        assert ledger2._current_hour == 14
        assert ledger2._current_date == "2026-03-30"
