"""Tests for custom_components.carmabox.optimizer.device_profiles."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.carmabox.const import (
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_EV_EFFICIENCY,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_VOLTAGE,
    DISHWASHER_AVG_KW,
    DISHWASHER_COOLDOWN_MIN,
    DISHWASHER_PEAK_KW,
    DISHWASHER_RUNTIME_H,
    EV_DAILY_ROLLING_DAYS,
)
from custom_components.carmabox.optimizer.device_profiles import (
    DeviceProfile,
    LoadSlot,
    Scenario,
    build_profiles,
    can_coexist,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _ev_profile(**kwargs: object) -> DeviceProfile:
    base = dict(
        name="ev",
        display_name="XPENG G9",
        power_kw=6.9,
        min_power_kw=4.14,
        max_power_kw=6.9,
        min_runtime_h=0.0,
        interruptible=True,
        cooldown_min=0,
        priority=1,
        consumer_type="variable",
        efficiency=DEFAULT_EV_EFFICIENCY,
        entity_switch=None,
        entity_power=None,
        capacity_kwh=82.0,
    )
    base.update(kwargs)
    return DeviceProfile(**base)  # type: ignore[arg-type]


def _battery_profile(name: str = "battery_kontor", capacity: float = 15.0) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        display_name="GoodWe Kontor",
        power_kw=3.6,
        min_power_kw=0.3,
        max_power_kw=3.6,
        min_runtime_h=0.0,
        interruptible=True,
        cooldown_min=0,
        priority=2,
        consumer_type="variable",
        efficiency=DEFAULT_BATTERY_EFFICIENCY,
        entity_switch=None,
        entity_power=None,
        capacity_kwh=capacity,
    )


def _simple_profile(name: str, priority: int = 5) -> DeviceProfile:
    return DeviceProfile(
        name=name,
        display_name=name.title(),
        power_kw=1.0,
        min_power_kw=1.0,
        max_power_kw=1.0,
        min_runtime_h=0.0,
        interruptible=True,
        cooldown_min=0,
        priority=priority,
        consumer_type="on_off",
        efficiency=1.0,
        entity_switch=None,
        entity_power=None,
        capacity_kwh=None,
    )


# ── DeviceProfile creation ─────────────────────────────────────────────────


class TestDeviceProfileCreation:
    def test_basic_fields(self) -> None:
        p = _ev_profile()
        assert p.name == "ev"
        assert p.display_name == "XPENG G9"
        assert p.power_kw == pytest.approx(6.9)
        assert p.capacity_kwh == pytest.approx(82.0)
        assert p.priority == 1
        assert p.consumer_type == "variable"
        assert p.efficiency == pytest.approx(DEFAULT_EV_EFFICIENCY)
        assert p.interruptible is True
        assert p.entity_switch is None
        assert p.entity_power is None

    def test_to_dict_contains_expected_keys(self) -> None:
        p = _ev_profile()
        d = p.to_dict()
        assert "name" in d
        assert "display_name" in d
        assert "power_kw" in d
        assert "min_power_kw" in d
        assert "max_power_kw" in d
        assert "min_runtime_h" in d
        assert "interruptible" in d
        assert "cooldown_min" in d
        assert "priority" in d
        assert "consumer_type" in d
        assert "efficiency" in d
        assert "entity_switch" in d
        assert "entity_power" in d
        assert "capacity_kwh" in d
        assert "avg_daily_consumption_kwh" in d

    def test_to_dict_values(self) -> None:
        p = _ev_profile()
        d = p.to_dict()
        assert d["name"] == "ev"
        assert d["priority"] == 1
        assert d["capacity_kwh"] == pytest.approx(82.0)
        assert d["avg_daily_consumption_kwh"] == pytest.approx(0.0)

    def test_frozen_prevents_field_reassignment(self) -> None:
        p = _ev_profile()
        with pytest.raises((TypeError, AttributeError)):
            p.name = "other"  # type: ignore[misc]

    def test_no_capacity_profile(self) -> None:
        p = _simple_profile("miner")
        assert p.capacity_kwh is None


# ── energy_needed ─────────────────────────────────────────────────────────


class TestEnergyNeeded:
    def test_ev_30_to_75_pct(self) -> None:
        """EV 30%→75%: netto = 36.9 kWh, AC = ~40.1 kWh."""
        p = _ev_profile(capacity_kwh=82.0, efficiency=DEFAULT_EV_EFFICIENCY)
        result = p.energy_needed(30.0, 75.0)
        expected_net = (75.0 - 30.0) / 100.0 * 82.0
        expected_ac = expected_net / DEFAULT_EV_EFFICIENCY
        assert result == pytest.approx(expected_ac, rel=1e-4)
        assert result == pytest.approx(40.108, rel=1e-2)

    def test_already_at_target_returns_zero(self) -> None:
        p = _ev_profile()
        assert p.energy_needed(75.0, 75.0) == pytest.approx(0.0)

    def test_above_target_returns_zero(self) -> None:
        p = _ev_profile()
        assert p.energy_needed(80.0, 75.0) == pytest.approx(0.0)

    def test_no_capacity_returns_zero(self) -> None:
        p = _simple_profile("miner")
        assert p.energy_needed(0.0, 100.0) == pytest.approx(0.0)

    def test_zero_to_full(self) -> None:
        """0%→100%: full capacity ÷ efficiency."""
        p = _battery_profile(capacity=15.0)
        result = p.energy_needed(0.0, 100.0)
        expected = 15.0 / DEFAULT_BATTERY_EFFICIENCY
        assert result == pytest.approx(expected, rel=1e-6)

    def test_efficiency_applied(self) -> None:
        """Lower efficiency → more AC energy needed."""
        p_eff = _ev_profile(efficiency=1.0)
        p_loss = _ev_profile(efficiency=0.8)
        net = p_eff.energy_needed(0.0, 50.0)
        lossy = p_loss.energy_needed(0.0, 50.0)
        assert lossy > net
        assert lossy == pytest.approx(net / 0.8, rel=1e-6)

    def test_small_increment(self) -> None:
        """1% increment on EV."""
        p = _ev_profile(capacity_kwh=82.0, efficiency=0.92)
        result = p.energy_needed(50.0, 51.0)
        assert result == pytest.approx(82.0 / 100.0 / 0.92, rel=1e-5)


# ── update_daily_consumption + avg ────────────────────────────────────────


class TestDailyConsumption:
    def test_empty_avg_is_zero(self) -> None:
        p = _ev_profile()
        assert p.avg_daily_consumption() == pytest.approx(0.0)

    def test_add_single_sample(self) -> None:
        p = _ev_profile()
        p.update_daily_consumption(20.0)
        assert p.avg_daily_consumption() == pytest.approx(20.0)

    def test_rolling_average(self) -> None:
        p = _ev_profile()
        for i in range(1, 5):
            p.update_daily_consumption(float(i) * 10)
        assert p.avg_daily_consumption() == pytest.approx(25.0)  # (10+20+30+40)/4

    def test_rolling_window_drops_oldest(self) -> None:
        p = _ev_profile()
        for _ in range(EV_DAILY_ROLLING_DAYS):
            p.update_daily_consumption(10.0)
        p.update_daily_consumption(99.0)
        # 7 samples: six 10s + one 99
        assert len(p._daily_consumption_samples) == EV_DAILY_ROLLING_DAYS
        assert p._daily_consumption_samples[-1] == pytest.approx(99.0)
        assert p._daily_consumption_samples[0] == pytest.approx(10.0)

    def test_window_never_exceeds_max(self) -> None:
        p = _ev_profile()
        for i in range(20):
            p.update_daily_consumption(float(i))
        assert len(p._daily_consumption_samples) == EV_DAILY_ROLLING_DAYS

    def test_to_dict_avg_updates(self) -> None:
        p = _ev_profile()
        p.update_daily_consumption(30.0)
        d = p.to_dict()
        assert d["avg_daily_consumption_kwh"] == pytest.approx(30.0)


# ── can_coexist ────────────────────────────────────────────────────────────


class TestCanCoexist:
    def setup_method(self) -> None:
        self.profiles = build_profiles({})

    def test_ev_battery_kontor_false(self) -> None:
        assert can_coexist(self.profiles["ev"], self.profiles["battery_kontor"]) is False

    def test_ev_battery_forrad_false(self) -> None:
        assert can_coexist(self.profiles["ev"], self.profiles["battery_forrad"]) is False

    def test_battery_ev_symmetric(self) -> None:
        assert can_coexist(self.profiles["battery_kontor"], self.profiles["ev"]) is False

    def test_dishwasher_ev_false(self) -> None:
        dw = _simple_profile("dishwasher")
        assert can_coexist(dw, self.profiles["ev"]) is False

    def test_dishwasher_battery_false(self) -> None:
        dw = _simple_profile("dishwasher")
        assert can_coexist(dw, self.profiles["battery_kontor"]) is False

    def test_dishwasher_battery_forrad_false(self) -> None:
        dw = _simple_profile("dishwasher")
        assert can_coexist(dw, self.profiles["battery_forrad"]) is False

    def test_ev_vp_kontor_true(self) -> None:
        assert can_coexist(self.profiles["ev"], self.profiles["vp_kontor"]) is True

    def test_ev_miner_true(self) -> None:
        assert can_coexist(self.profiles["ev"], self.profiles["miner"]) is True

    def test_miner_pool_heater_true(self) -> None:
        assert can_coexist(self.profiles["miner"], self.profiles["pool_heater"]) is True

    def test_battery_kontor_battery_forrad_true(self) -> None:
        """Two batteries can charge simultaneously."""
        assert can_coexist(self.profiles["battery_kontor"], self.profiles["battery_forrad"]) is True

    def test_same_device_true(self) -> None:
        p = self.profiles["miner"]
        assert can_coexist(p, p) is True

    def test_vp_pool_pool_heater_true(self) -> None:
        assert can_coexist(self.profiles["vp_pool"], self.profiles["pool_heater"]) is True


# ── build_profiles ─────────────────────────────────────────────────────────


class TestBuildProfiles:
    def test_returns_all_eight_profiles(self) -> None:
        profiles = build_profiles({})
        expected = {"ev", "battery_kontor", "battery_forrad", "vp_kontor",
                    "vp_pool", "pool_heater", "miner", "dishwasher"}
        assert set(profiles.keys()) == expected

    def test_ev_defaults(self) -> None:
        p = build_profiles({})["ev"]
        ev_max_kw = DEFAULT_EV_MAX_AMPS * 3 * DEFAULT_VOLTAGE / 1000.0
        ev_min_kw = DEFAULT_EV_MIN_AMPS * 3 * DEFAULT_VOLTAGE / 1000.0
        assert p.max_power_kw == pytest.approx(ev_max_kw)
        assert p.min_power_kw == pytest.approx(ev_min_kw)
        assert p.efficiency == pytest.approx(DEFAULT_EV_EFFICIENCY)
        assert p.priority == 1
        assert p.consumer_type == "variable"
        assert p.capacity_kwh == pytest.approx(82.0)

    def test_ev_max_kw_is_69(self) -> None:
        """MAX_EV_CURRENT=10A × 3phases × 230V = 6.9 kW."""
        p = build_profiles({})["ev"]
        assert p.max_power_kw == pytest.approx(6.9, abs=0.01)

    def test_battery_kontor_defaults(self) -> None:
        p = build_profiles({})["battery_kontor"]
        assert p.efficiency == pytest.approx(DEFAULT_BATTERY_EFFICIENCY)
        assert p.capacity_kwh == pytest.approx(DEFAULT_BATTERY_1_KWH)
        assert p.priority == 2

    def test_battery_forrad_defaults(self) -> None:
        p = build_profiles({})["battery_forrad"]
        assert p.capacity_kwh == pytest.approx(DEFAULT_BATTERY_2_KWH)
        assert p.priority == 2

    def test_dishwasher_defaults(self) -> None:
        p = build_profiles({})["dishwasher"]
        assert p.power_kw == pytest.approx(DISHWASHER_AVG_KW)
        assert p.max_power_kw == pytest.approx(DISHWASHER_PEAK_KW)
        assert p.min_runtime_h == pytest.approx(DISHWASHER_RUNTIME_H)
        assert p.cooldown_min == DISHWASHER_COOLDOWN_MIN
        assert p.interruptible is False
        assert p.priority == 7

    def test_config_override_entity_switch(self) -> None:
        cfg = {"ev": {"entity_switch": "switch.easee_car"}}
        p = build_profiles(cfg)["ev"]
        assert p.entity_switch == "switch.easee_car"

    def test_config_override_capacity(self) -> None:
        cfg = {"ev": {"capacity_kwh": 100.0}}
        p = build_profiles(cfg)["ev"]
        assert p.capacity_kwh == pytest.approx(100.0)

    def test_config_override_does_not_affect_others(self) -> None:
        cfg = {"ev": {"priority": 99}}
        profiles = build_profiles(cfg)
        assert profiles["battery_kontor"].priority == 2

    def test_name_set_correctly(self) -> None:
        profiles = build_profiles({})
        for name, p in profiles.items():
            assert p.name == name

    def test_priorities_unique_except_batteries(self) -> None:
        profiles = build_profiles({})
        prios = [p.priority for p in profiles.values()]
        # Both batteries have priority 2 — all others unique
        from collections import Counter
        c = Counter(prios)
        assert c[2] == 2  # battery_kontor + battery_forrad
        for prio, count in c.items():
            if prio != 2:
                assert count == 1


# ── LoadSlot ───────────────────────────────────────────────────────────────


class TestLoadSlot:
    def test_basic_creation(self) -> None:
        slot = LoadSlot(hour=14, device="ev", power_kw=6.9)
        assert slot.hour == 14
        assert slot.device == "ev"
        assert slot.power_kw == pytest.approx(6.9)

    def test_default_duration(self) -> None:
        slot = LoadSlot(hour=0, device="miner", power_kw=0.5)
        assert slot.duration_min == 60

    def test_default_reason(self) -> None:
        slot = LoadSlot(hour=0, device="miner", power_kw=0.5)
        assert slot.reason == ""

    def test_custom_duration_and_reason(self) -> None:
        slot = LoadSlot(hour=3, device="battery_kontor", power_kw=3.6,
                        duration_min=30, reason="cheap rate")
        assert slot.duration_min == 30
        assert slot.reason == "cheap rate"

    def test_frozen(self) -> None:
        slot = LoadSlot(hour=1, device="ev", power_kw=6.9)
        with pytest.raises((TypeError, AttributeError)):
            slot.hour = 2  # type: ignore[misc]


# ── Scenario ──────────────────────────────────────────────────────────────


class TestScenario:
    def test_basic_creation(self) -> None:
        s = Scenario(name="ev_heavy", ev_target_soc=80.0, battery_target_soc=90.0)
        assert s.name == "ev_heavy"
        assert s.ev_target_soc == pytest.approx(80.0)
        assert s.battery_target_soc == pytest.approx(90.0)
        assert s.total_cost_kr == pytest.approx(0.0)

    def test_total_energy_kwh_empty(self) -> None:
        s = Scenario(name="empty")
        assert s.total_energy_kwh == pytest.approx(0.0)

    def test_total_energy_kwh_single_slot(self) -> None:
        """6.9 kW for 60 min = 6.9 kWh."""
        slot = LoadSlot(hour=14, device="ev", power_kw=6.9, duration_min=60)
        s = Scenario(name="test", slots=[slot])
        assert s.total_energy_kwh == pytest.approx(6.9)

    def test_total_energy_kwh_multiple_slots(self) -> None:
        slots = [
            LoadSlot(hour=1, device="ev", power_kw=6.9, duration_min=60),
            LoadSlot(hour=2, device="battery_kontor", power_kw=3.6, duration_min=30),
            LoadSlot(hour=3, device="miner", power_kw=0.5, duration_min=120),
        ]
        s = Scenario(name="multi", slots=slots)
        expected = 6.9 * 1.0 + 3.6 * 0.5 + 0.5 * 2.0
        assert s.total_energy_kwh == pytest.approx(expected)

    def test_to_dict_keys(self) -> None:
        s = Scenario(name="balanced")
        d = s.to_dict()
        assert "name" in d
        assert "total_cost_kr" in d
        assert "total_energy_kwh" in d
        assert "ev_target_soc" in d
        assert "battery_target_soc" in d
        assert "created_at" in d
        assert "slots" in d

    def test_to_dict_slots_serialized(self) -> None:
        slot = LoadSlot(hour=5, device="ev", power_kw=6.9, duration_min=60, reason="cheap")
        s = Scenario(name="test", slots=[slot])
        d = s.to_dict()
        assert len(d["slots"]) == 1
        assert d["slots"][0]["hour"] == 5
        assert d["slots"][0]["device"] == "ev"
        assert d["slots"][0]["reason"] == "cheap"

    def test_to_dict_created_at_is_iso_string(self) -> None:
        s = Scenario(name="test")
        d = s.to_dict()
        # Should parse back to datetime without error
        datetime.fromisoformat(d["created_at"])

    def test_created_at_defaults_to_now(self) -> None:
        before = datetime.now()
        s = Scenario(name="test")
        after = datetime.now()
        assert before <= s.created_at <= after

    def test_custom_created_at(self) -> None:
        ts = datetime(2026, 1, 15, 12, 0, 0)
        s = Scenario(name="test", created_at=ts)
        assert s.created_at == ts

    def test_total_cost_set(self) -> None:
        s = Scenario(name="expensive", total_cost_kr=42.50)
        assert s.total_cost_kr == pytest.approx(42.50)
