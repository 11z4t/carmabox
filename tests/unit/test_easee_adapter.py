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
from custom_components.carmabox.const import DEFAULT_EV_MAX_AMPS


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
    """AC: set_current clamps to [_DYNAMIC_MIN, DEFAULT_EV_MAX_AMPS]."""

    @pytest.mark.asyncio
    async def test_set_current_clamp_upper(self) -> None:
        """set_current(20) → clamped to DEFAULT_EV_MAX_AMPS (10A)."""
        hass = _make_hass()
        adapter = EaseeAdapter(hass, "dev1", PREFIX, charger_id=CHARGER_ID)
        await adapter.set_current(20)
        # Last call = set_charger_dynamic_limit with clamped value
        last_call = hass.services.async_call.call_args
        assert last_call[0] == (
            "easee",
            "set_charger_dynamic_limit",
            {"charger_id": CHARGER_ID, "current": DEFAULT_EV_MAX_AMPS},
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

        await adapter.set_current(DEFAULT_EV_MAX_AMPS)
        last_call = hass.services.async_call.call_args
        assert last_call[0][2]["current"] == DEFAULT_EV_MAX_AMPS


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
