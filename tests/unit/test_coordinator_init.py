"""Coverage tests for CarmaboxCoordinator.__init__ branches.

EXP-EPIC-SWEEP — targets coordinator.py init clusters:
  Lines 161-184   — Core init + EV SoC seed from helper
  Lines 200-217   — Inverter + EV adapter setup
  Lines 224-308   — Weather adapter + savings + predictor + ledger etc.
  Lines 314-326   — License features from config
  Lines 350-363   — Appliances + miner entity setup
  Lines 368-391   — Flat-line controller + daily goals
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.carmabox.coordinator import CarmaboxCoordinator

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_hass(*, ev_soc_seed: str | None = None) -> MagicMock:
    """Create minimal hass mock for __init__ testing."""
    hass = MagicMock()
    hass.data = {}

    states: dict[str, MagicMock] = {}
    if ev_soc_seed is not None:
        s = MagicMock()
        s.state = ev_soc_seed
        states["input_number.carma_ev_last_known_soc"] = s

    def _get(entity_id: str) -> MagicMock | None:
        return states.get(entity_id)

    hass.states.get = _get
    hass.states.async_all = MagicMock(return_value=[])
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config = MagicMock()
    hass.async_create_task = MagicMock(return_value=MagicMock())
    return hass


def _make_entry(options: dict | None = None) -> MagicMock:
    """Create minimal ConfigEntry mock."""
    entry = MagicMock()
    cfg = options or {}
    entry.data = cfg
    entry.options = {}
    entry.entry_id = "test_init"
    return entry


# ── Basic __init__ ────────────────────────────────────────────────────────────

class TestCoordinatorInit:
    """Lines 161-391: __init__ branch coverage."""

    def test_basic_init_no_ev_no_inverter(self) -> None:
        """Default config → coordinator initializes without EV or inverter adapters."""
        hass = _make_hass()
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._cfg == {}
        assert coord.plan == []
        assert coord.ev_adapter is None
        assert coord.inverter_adapters == []

    def test_ev_soc_seeded_from_helper(self) -> None:
        """input_number.carma_ev_last_known_soc state → seeds _last_known_ev_soc (lines 182-187)."""
        hass = _make_hass(ev_soc_seed="75")
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._last_known_ev_soc == 75.0

    def test_ev_soc_seed_unknown_state(self) -> None:
        """Helper state = 'unknown' → skip seeding (line 182 condition false)."""
        hass = _make_hass(ev_soc_seed="unknown")
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._last_known_ev_soc == -1.0  # Default

    def test_ev_soc_seed_unavailable_state(self) -> None:
        """Helper state = 'unavailable' → skip seeding."""
        hass = _make_hass(ev_soc_seed="unavailable")
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._last_known_ev_soc == -1.0

    def test_ev_soc_seed_empty_state(self) -> None:
        """Helper state = '' → skip seeding."""
        hass = _make_hass(ev_soc_seed="")
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._last_known_ev_soc == -1.0

    def test_inverter_adapter_created_with_prefix(self) -> None:
        """inverter_1_prefix set → GoodWeAdapter added to inverter_adapters (lines 204-208)."""
        hass = _make_hass()
        entry = _make_entry({"inverter_1_prefix": "gw1", "inverter_1_device_id": "dev1"})
        coord = CarmaboxCoordinator(hass, entry)

        assert len(coord.inverter_adapters) == 1

    def test_two_inverter_adapters_with_both_prefixes(self) -> None:
        """Both inverter_1_prefix and inverter_2_prefix → 2 adapters."""
        hass = _make_hass()
        entry = _make_entry({
            "inverter_1_prefix": "gw1",
            "inverter_1_device_id": "dev1",
            "inverter_2_prefix": "gw2",
            "inverter_2_device_id": "dev2",
        })
        coord = CarmaboxCoordinator(hass, entry)

        assert len(coord.inverter_adapters) == 2

    def test_ev_adapter_created_when_enabled(self) -> None:
        """ev_enabled + ev_prefix → EaseeAdapter set (lines 212-219)."""
        hass = _make_hass()
        entry = _make_entry({
            "ev_enabled": True,
            "ev_prefix": "easee_home_test",
            "ev_device_id": "dev_ev",
            "ev_charger_id": "charger1",
        })
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.ev_adapter is not None

    def test_no_ev_adapter_when_disabled(self) -> None:
        """ev_enabled=False → ev_adapter stays None (line 212 condition false)."""
        hass = _make_hass()
        entry = _make_entry({"ev_enabled": False})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.ev_adapter is None

    def test_weather_adapter_created_by_default(self) -> None:
        """weather_enabled defaults True → weather_adapter is set (lines 224-226)."""
        hass = _make_hass()
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        # Default: weather_enabled=True → TempestAdapter created
        assert coord.weather_adapter is not None

    def test_weather_adapter_disabled(self) -> None:
        """weather_enabled=False → weather_adapter is None."""
        hass = _make_hass()
        entry = _make_entry({"weather_enabled": False})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.weather_adapter is None

    def test_license_features_from_config(self) -> None:
        """license_features in config → _license_features set (line 315)."""
        hass = _make_hass()
        entry = _make_entry({
            "license_features": ["analyzer", "executor", "ev_control"]
        })
        coord = CarmaboxCoordinator(hass, entry)

        assert "executor" in coord._license_features
        assert "ev_control" in coord._license_features

    def test_executor_enabled_when_licensed(self) -> None:
        """executor_enabled=True + no hub_url → premium auto-grant → executor_enabled=True."""
        hass = _make_hass()
        # No hub_url → dev/owner mode → all features auto-granted (lines 322-337)
        entry = _make_entry({"executor_enabled": True})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.executor_enabled is True

    def test_executor_disabled_when_config_false(self) -> None:
        """executor_enabled=False in config → executor_enabled=False (line 341)."""
        hass = _make_hass()
        entry = _make_entry({"executor_enabled": False})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.executor_enabled is False

    def test_min_soc_from_config(self) -> None:
        """min_soc in config → coord.min_soc set (line 229)."""
        hass = _make_hass()
        entry = _make_entry({"min_soc": 25.0})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.min_soc == 25.0

    def test_target_kw_from_config(self) -> None:
        """target_weighted_kw in config → coord.target_kw set (line 228)."""
        hass = _make_hass()
        entry = _make_entry({"target_weighted_kw": 3.5})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord.target_kw == 3.5

    def test_appliances_from_config(self) -> None:
        """appliances in config → _appliances populated (line 353)."""
        hass = _make_hass()
        entry = _make_entry({
            "appliances": [
                {"entity_id": "sensor.disk_power", "category": "dishwasher"},
                {"entity_id": "switch.shelly_miner", "category": "miner"},
            ]
        })
        coord = CarmaboxCoordinator(hass, entry)

        assert len(coord._appliances) == 2

    def test_miner_entity_from_config(self) -> None:
        """miner_entity in config → _miner_entity set (line 360)."""
        hass = _make_hass()
        entry = _make_entry({"miner_entity": "switch.my_miner"})
        coord = CarmaboxCoordinator(hass, entry)

        assert coord._miner_entity == "switch.my_miner"

    def test_savings_initialized_with_current_month_year(self) -> None:
        """savings initialized with current month/year (line 231)."""
        hass = _make_hass()
        entry = _make_entry()
        coord = CarmaboxCoordinator(hass, entry)

        from datetime import datetime
        now = datetime.now()
        assert coord.savings.month == now.month
        assert coord.savings.year == now.year
