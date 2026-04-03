"""Tests for EaseeAdapter — PLAT-1045.

Covers safety-critical clamp logic, idempotent init, reset,
and charger_id vs entity fallback path.
All tests run without real HA imports (mock hass).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.adapters.easee import (
    _DYNAMIC_MIN,
    _MAX_LIMIT_FLOOR,
    EaseeAdapter,
)
from custom_components.carmabox.const import MAX_EV_CURRENT


def _make_hass(*entities: tuple[str, str]) -> MagicMock:
    """Create mock hass with states."""
    hass = MagicMock()
    states: dict[str, MagicMock] = {}
    for entity_id, value in entities:
        state = MagicMock()
        state.state = value
        state.attributes = {}
        states[entity_id] = state

    hass.states.get = lambda eid: states.get(eid)
    hass.services.async_call = AsyncMock()
    return hass


PREFIX = "easee_home_12840"
CHARGER_ID = "EH12840"


class TestSetCurrentClamp:
    """AC: set_current clamps to [_DYNAMIC_MIN, MAX_EV_CURRENT]."""

    @pytest.mark.asyncio
    async def test_set_current_clamp_upper(self) -> None:
        """set_current(20) → clamped to MAX_EV_CURRENT (10A)."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(20)
        # Last call = set_charger_dynamic_limit with clamped value
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": MAX_EV_CURRENT},
        )

    @pytest.mark.asyncio
    async def test_set_current_clamp_lower(self) -> None:
        """set_current(3) → clamped to _DYNAMIC_MIN (6A)."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(3)
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": _DYNAMIC_MIN},
        )

    @pytest.mark.asyncio
    async def test_set_current_passthrough_mid(self) -> None:
        """set_current(8) → passes through unchanged."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(8)
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": 8},
        )

    @pytest.mark.asyncio
    async def test_set_current_at_boundaries(self) -> None:
        """set_current at exact min/max passes through."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(_DYNAMIC_MIN)
        last_call = hass.services.async_call.call_args
        assert last_call[0][2]["current"] == _DYNAMIC_MIN

        await adapter.set_current(MAX_EV_CURRENT)
        last_call = hass.services.async_call.call_args
        assert last_call[0][2]["current"] == MAX_EV_CURRENT


class TestEnsureInitialized:
    """AC: ensure_initialized() is idempotent — API called max once."""

    @pytest.mark.asyncio
    async def test_ensure_initialized_idempotent(self) -> None:
        """Called 3 times → service calls happen only on first invocation."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)

        await adapter.ensure_initialized()
        first_call_count = hass.services.async_call.call_count

        await adapter.ensure_initialized()
        await adapter.ensure_initialized()

        # Call count unchanged after first init
        assert hass.services.async_call.call_count == first_call_count
        assert first_call_count > 0  # Sanity: at least one call was made

    @pytest.mark.asyncio
    async def test_ensure_initialized_sets_max_and_dynamic(self) -> None:
        """Init sets max_limit=10A, dynamic=6A, circuit=10A, smart_charging=off."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.ensure_initialized()

        call_args = [c[0] for c in hass.services.async_call.call_args_list]
        # max_limit
        assert (
            "easee",
            "set_charger_max_limit",
            {"charger_id": CHARGER_ID, "current": _MAX_LIMIT_FLOOR},
        ) in call_args
        # dynamic
        assert (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": _DYNAMIC_MIN},
        ) in call_args
        # circuit
        assert (
            "easee",
            "set_circuit_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": _MAX_LIMIT_FLOOR},
        ) in call_args
        # smart_charging off
        assert (
            "switch",
            "turn_off",
            {"entity_id": f"switch.{PREFIX}_smart_charging"},
        ) in call_args


class TestResetToDefault:
    """AC: reset_to_default sets dynamic limit to _DYNAMIC_MIN."""

    @pytest.mark.asyncio
    async def test_reset_to_default(self) -> None:
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        result = await adapter.reset_to_default()
        assert result is True
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": _DYNAMIC_MIN},
        )


class TestChargerIdFallback:
    """AC: charger_id path vs entity fallback path."""

    @pytest.mark.asyncio
    async def test_set_current_with_charger_id(self) -> None:
        """With charger_id → uses easee.set_charger_dynamic_limit service."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(8)
        last_call = hass.services.async_call.call_args
        assert last_call[0][0] == "easee"
        assert last_call[0][1] == "set_charger_dynamic_limit"
        assert last_call[0][2]["charger_id"] == CHARGER_ID

    @pytest.mark.asyncio
    async def test_set_current_without_charger_id_uses_entity(self) -> None:
        """Without charger_id → falls back to number.set_value entity."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id="")
        await adapter.set_current(8)
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "number",
            "set_value",
            {"entity_id": f"number.{PREFIX}_dynamic_charger_limit", "value": 8},
        )

    @pytest.mark.asyncio
    async def test_init_without_charger_id_skips_easee_services(self) -> None:
        """Without charger_id → ensure_initialized skips Easee-specific services."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id="")
        await adapter.ensure_initialized()
        call_args = [c[0] for c in hass.services.async_call.call_args_list]
        # Only smart_charging off should be called
        assert len(call_args) == 1
        assert call_args[0] == (
            "switch",
            "turn_off",
            {"entity_id": f"switch.{PREFIX}_smart_charging"},
        )

    @pytest.mark.asyncio
    async def test_enable_with_charger_id_sends_resume(self) -> None:
        """With charger_id → enable sends resume action_command."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.enable()
        call_args = [c[0] for c in hass.services.async_call.call_args_list]
        assert (
            "easee",
            "action_command",
            {"charger_id": CHARGER_ID, "action_command": "resume"},
        ) in call_args

    @pytest.mark.asyncio
    async def test_enable_without_charger_id_no_resume(self) -> None:
        """Without charger_id → enable does NOT send resume."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id="")
        await adapter.enable()
        call_args = [c[0] for c in hass.services.async_call.call_args_list]
        resume_calls = [c for c in call_args if len(c) >= 2 and c[1] == "action_command"]
        assert len(resume_calls) == 0


SHELLY_PREFIX = "shellypro3em_8813bffea620"


class TestConnectionState:
    """EXP-10: EV connection state tracking."""

    def test_state_charging(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_status", "charging"),
            (f"binary_sensor.{PREFIX}_plug", "on"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.connection_state == "charging"

    def test_state_connected(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_status", "awaiting_start"),
            (f"binary_sensor.{PREFIX}_plug", "on"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.connection_state == "connected"

    def test_state_disconnected(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_status", "disconnected"),
            (f"binary_sensor.{PREFIX}_plug", "off"),
            (f"binary_sensor.{PREFIX}_cable_locked", "off"),
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.connection_state == "disconnected"

    def test_state_error(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_status", "error"),
            (f"binary_sensor.{PREFIX}_plug", "off"),
            (f"binary_sensor.{PREFIX}_cable_locked", "off"),
            (f"sensor.{PREFIX}_reason_for_no_current", "51"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.connection_state == "error"

    def test_unexpected_disconnect_detected(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_status", "disconnected"),
            (f"binary_sensor.{PREFIX}_plug", "off"),
            (f"binary_sensor.{PREFIX}_cable_locked", "off"),
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        alert = adapter.check_unexpected_disconnect(was_charging=True)
        assert alert is not None
        assert "Unexpected" in alert

    def test_no_alert_when_not_charging(self) -> None:
        hass = _make_hass(
            (f"binary_sensor.{PREFIX}_plug", "off"),
            (f"binary_sensor.{PREFIX}_cable_locked", "off"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.check_unexpected_disconnect(was_charging=False) is None

    def test_no_alert_when_still_connected(self) -> None:
        hass = _make_hass(
            (f"binary_sensor.{PREFIX}_plug", "on"),
            (f"sensor.{PREFIX}_status", "awaiting_start"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.check_unexpected_disconnect(was_charging=True) is None


class TestReasonForNoCurrentRecovery:
    """EXP-05: Reason-for-no-current monitoring + auto-recovery."""

    def test_needs_recovery_waiting_in_fully(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", "51"),
            (f"sensor.{PREFIX}_max_charger_limit", "10"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.needs_recovery is True

    def test_needs_recovery_max_limit_low(self) -> None:
        """max_charger_limit < 10A indicates reboot/corruption."""
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
            (f"sensor.{PREFIX}_max_charger_limit", "6"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.needs_recovery is True

    def test_no_recovery_needed_normal(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
            (f"sensor.{PREFIX}_max_charger_limit", "10"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.needs_recovery is False

    @pytest.mark.asyncio
    async def test_try_recover_waiting_in_fully(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", "51"),
            (f"sensor.{PREFIX}_max_charger_limit", "10"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        result = await adapter.try_recover()
        assert result == "waiting_in_fully_fix"
        # Should have called ensure_initialized
        assert hass.services.async_call.call_count > 0

    @pytest.mark.asyncio
    async def test_try_recover_reboot(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
            (f"sensor.{PREFIX}_max_charger_limit", "6"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        result = await adapter.try_recover()
        assert result == "reboot_reinit"

    @pytest.mark.asyncio
    async def test_try_recover_none_when_ok(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", ""),
            (f"sensor.{PREFIX}_max_charger_limit", "10"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        result = await adapter.try_recover()
        assert result is None

    @pytest.mark.asyncio
    async def test_try_recover_circuit_low(self) -> None:
        hass = _make_hass(
            (f"sensor.{PREFIX}_reason_for_no_current", "6"),
            (f"sensor.{PREFIX}_max_charger_limit", "10"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        result = await adapter.try_recover()
        assert result == "circuit_low_fix"

    def test_max_charger_limit_reading(self) -> None:
        hass = _make_hass((f"sensor.{PREFIX}_max_charger_limit", "16"))
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.max_charger_limit_a == 16.0


class TestShellyPro3EMIntegration:
    """EXP-01: Shelly Pro 3EM as primary EV power sensor."""

    def test_power_w_prefers_shelly_over_easee(self) -> None:
        """When Shelly reads >10W, power_w returns Shelly value."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "10.0"),  # 10A x 230V = 2300W
            (f"sensor.{PREFIX}_power", "2.1"),  # Easee: 2100W
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        assert adapter.power_w == 2300.0  # Shelly wins

    def test_power_w_falls_back_to_easee_when_shelly_zero(self) -> None:
        """When Shelly reads 0W (EV off), falls back to Easee."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "0.0"),
            (f"sensor.{PREFIX}_power", "1.5"),  # Easee: 1500W
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        assert adapter.power_w == 1500.0  # Easee fallback

    def test_power_w_falls_back_when_shelly_unavailable(self) -> None:
        """When Shelly entity unavailable, uses Easee."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "unavailable"),
            (f"sensor.{PREFIX}_power", "2.0"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        assert adapter.power_w == 2000.0

    def test_power_w_without_shelly_uses_easee(self) -> None:
        """No Shelly configured → always use Easee sensor."""
        hass = _make_hass(
            (f"sensor.{PREFIX}_power", "3.0"),
        )
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.power_w == 3000.0

    def test_shelly_power_w_returns_zero_without_prefix(self) -> None:
        """shelly_power_w = 0 when no prefix configured."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.shelly_power_w == 0.0

    def test_shelly_phase_powers_per_phase(self) -> None:
        """Per-phase power from Shelly Pro 3EM — phase C is primary for XPENG."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_a_current", "0.5"),
            (f"sensor.{SHELLY_PREFIX}_a_voltage", "231.0"),
            (f"sensor.{SHELLY_PREFIX}_b_current", "0.3"),
            (f"sensor.{SHELLY_PREFIX}_b_voltage", "230.0"),
            (f"sensor.{SHELLY_PREFIX}_c_current", "15.8"),
            (f"sensor.{SHELLY_PREFIX}_c_voltage", "229.0"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        phases = adapter.shelly_phase_powers_w
        assert abs(phases["a"] - 115.5) < 1
        assert abs(phases["b"] - 69.0) < 1
        assert abs(phases["c"] - 3618.2) < 1  # Primary EV phase

    def test_shelly_phase_powers_empty_without_prefix(self) -> None:
        """No Shelly configured → empty dict."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        assert adapter.shelly_phase_powers_w == {}

    def test_power_kw_derived_from_power_w(self) -> None:
        """power_kw = power_w / 1000."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "6.5"),
            (f"sensor.{PREFIX}_power", "0.5"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        assert abs(adapter.power_kw - 1.495) < 0.01  # 6.5A x 230V / 1000

    # ── EXP-EPIC-SWEEP edge cases ─────────────────────────────────

    def test_power_w_shelly_below_threshold_falls_back(self) -> None:
        """EXP-01 edge: Shelly = 9.2W (below 10W threshold) → falls back to Easee.

        The threshold is `shelly > 10` (strict greater-than).
        Readings ≤ 10W are treated as noise and trigger Easee fallback.
        """
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "0.04"),  # 0.04A * 230V = 9.2W ≤ 10
            (f"sensor.{PREFIX}_power", "2.0"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        # shelly_power_w = 9.2W → NOT > 10 → falls back to Easee (2000W)
        assert adapter.power_w == 2000.0

    def test_power_w_shelly_just_above_threshold_wins(self) -> None:
        """EXP-01 edge: Shelly = 10.5W (just above threshold) → Shelly wins."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "0.0457"),  # ~10.5W
            (f"sensor.{PREFIX}_power", "2.0"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        shelly_w = 0.0457 * 230  # ≈ 10.511W > 10 → Shelly wins
        assert abs(adapter.power_w - shelly_w) < 0.1

    def test_shelly_phase_powers_with_unavailable_voltage_defaults_to_230(self) -> None:
        """EXP-01 edge: phase voltage unavailable → defaults to 230V."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_c_current", "10.0"),
            # No c_voltage entity → state_by_id returns default=230.0
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        phases = adapter.shelly_phase_powers_w
        # Missing voltage → default 230V * 10A = 2300W
        assert phases["c"] == 2300.0

    def test_power_w_both_shelly_and_easee_zero(self) -> None:
        """EXP-01 edge: both Shelly and Easee show 0W → returns 0."""
        hass = _make_hass(
            (f"sensor.{SHELLY_PREFIX}_current", "0.0"),
            (f"sensor.{PREFIX}_power", "0.0"),
        )
        adapter = EaseeAdapter(
            hass, "dev1", PREFIX, charger_id=CHARGER_ID, shelly_3em_prefix=SHELLY_PREFIX
        )
        assert adapter.power_w == 0.0
