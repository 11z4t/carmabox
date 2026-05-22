"""S5 — distribution sensor reflects exact ems_limit write (signed).

The template sensor.bat_balancer_distribution_{kontor,forrad}_w was computed
by a separate Jinja algorithm → wrong values (e.g. -800W while ems_limit=88W).
Fix: Python BatBalancerDistributionSensor reads coordinator._last_ems_limit_w.

TC-S5-CHARGE:    _write_power_limit(bank, -3000) → distribution_w[bank] = -3000
TC-S5-DISCHARGE: _write_power_limit(bank, +2000) → distribution_w[bank] = +2000
TC-S5-STANDBY:   _write_power_limit(bank, 0)     → distribution_w[bank] = 0
TC-S5-BOTH_BANKS: both banks written → both distribution values correct
TC-S5-MAGNITUDE: fractional target rounds to nearest int (sign correct)
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.bat_balancer.const import (
    BANKS,
)
from custom_components.bat_balancer.coordinator import BatBalancerCoordinator
from custom_components.bat_balancer.models import BankConfig, BatBalancerState
from custom_components.bat_balancer.sign_state_machine import SignStateMachine


def _make_coord() -> BatBalancerCoordinator:
    hass = MagicMock()
    hass.data = {}
    hass.services.async_call = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "test"
    coord = BatBalancerCoordinator.__new__(BatBalancerCoordinator)
    coord.hass = hass
    coord._entry = entry
    coord._bank_configs = {
        "kontor": BankConfig.default_kontor(),
        "forrad": BankConfig.default_forrad(),
    }
    coord._sign_machines = {bid: SignStateMachine() for bid in BANKS}
    coord._state = BatBalancerState()
    coord._last_brain_write_ts = time.monotonic()
    coord._bank_was_online = {bid: True for bid in BANKS}
    coord._last_goodwe_ems_modes = {bid: None for bid in BANKS}
    coord._last_goodwe_modes = {bid: None for bid in BANKS}
    coord._last_written_ems_modes = {bid: None for bid in BANKS}
    coord._hw_mismatch_ticks = {bid: 0 for bid in BANKS}
    coord._full_bias_active = False
    coord._last_ems_limit_w = {bid: 0.0 for bid in BANKS}
    return coord


# ---------------------------------------------------------------------------
# TC-S5-CHARGE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_charge() -> None:
    """Write -3000W (charge) → distribution_w[kontor] = -3000."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", -3000.0)
    assert (
        coord.distribution_w["kontor"] == -3000.0
    ), f"Expected -3000, got {coord.distribution_w['kontor']}"
    assert coord.distribution_w["forrad"] == 0.0  # unchanged


# ---------------------------------------------------------------------------
# TC-S5-DISCHARGE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_discharge() -> None:
    """Write +2000W (discharge) → distribution_w[forrad] = +2000."""
    coord = _make_coord()
    await coord._write_power_limit("forrad", 2000.0)
    assert (
        coord.distribution_w["forrad"] == 2000.0
    ), f"Expected +2000, got {coord.distribution_w['forrad']}"
    assert coord.distribution_w["kontor"] == 0.0  # unchanged


# ---------------------------------------------------------------------------
# TC-S5-STANDBY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_standby() -> None:
    """Write 0W → distribution_w = 0 (ems_mode=battery_standby, sign=0)."""
    coord = _make_coord()
    # Pre-set to non-zero to verify it updates
    coord._last_ems_limit_w["kontor"] = -999.0
    await coord._write_power_limit("kontor", 0.0)
    assert (
        coord.distribution_w["kontor"] == 0.0
    ), f"Expected 0, got {coord.distribution_w['kontor']}"


# ---------------------------------------------------------------------------
# TC-S5-BOTH_BANKS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_both_banks() -> None:
    """Both banks written with different directions → both values correct."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", -1500.0)  # charge
    await coord._write_power_limit("forrad", 800.0)  # discharge
    assert coord.distribution_w["kontor"] == -1500.0
    assert coord.distribution_w["forrad"] == 800.0


# ---------------------------------------------------------------------------
# TC-S5-MAGNITUDE: fractional rounding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_rounds_to_int() -> None:
    """Target 2500.6W discharge → rounds to 2501 (round half-up)."""
    coord = _make_coord()
    await coord._write_power_limit("kontor", 2500.6)
    assert (
        coord.distribution_w["kontor"] == 2501.0
    ), f"Expected 2501, got {coord.distribution_w['kontor']}"


# ---------------------------------------------------------------------------
# TC-S5-W2-CLAMP: W2-clamped bank shows 0, not the global direction value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_sensor_w2_clamp_shows_zero() -> None:
    """W2: global discharge+0W per-bank → standby. Sensor must show 0W (not +discharge)."""
    coord = _make_coord()
    # direction_w=2000 (discharge), but per-bank target=0 → W2 clamps to standby
    await coord._write_power_limit("forrad", 0.0, direction_w=2000.0)
    assert (
        coord.distribution_w["forrad"] == 0.0
    ), f"W2-clamped bank must show 0W (standby), got {coord.distribution_w['forrad']}"
