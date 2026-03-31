"""Coverage tests for repairs.py async flow methods.

Targets lines 39, 46-52, 69-77, 81-85, 96-101:
  SafetyGuardRepairFlow.async_step_init
  SafetyGuardRepairFlow.async_step_confirm (with/without user_input)
  SafetyGuardRepairFlow._increase_min_soc (entries present/absent)
  SafetyGuardRepairFlow._get_placeholders (entries present/absent)
  async_create_fix_flow (both issue_id branches)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_flow_instance():
    """Create a SafetyGuardRepairFlow with mocked hass."""
    from custom_components.carmabox.repairs import SafetyGuardRepairFlow

    flow = SafetyGuardRepairFlow.__new__(SafetyGuardRepairFlow)
    hass = MagicMock()
    entry = MagicMock()
    entry.options = {}
    hass.config_entries.async_entries.return_value = [entry]
    hass.config_entries.async_update_entry = MagicMock()
    flow.hass = hass
    # Stub base-class methods
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    return flow, entry


# ── Tests: async_step_init ────────────────────────────────────────────────────


class TestAsyncStepInit:
    """Line 39: async_step_init delegates to async_step_confirm."""

    @pytest.mark.asyncio
    async def test_step_init_calls_confirm(self) -> None:
        flow, _ = _make_flow_instance()
        flow.async_step_confirm = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_init()
        flow.async_step_confirm.assert_awaited_once()
        assert result == {"type": "form"}


# ── Tests: async_step_confirm ─────────────────────────────────────────────────


class TestAsyncStepConfirm:
    """Lines 46-52: confirm with / without user_input."""

    @pytest.mark.asyncio
    async def test_confirm_no_input_shows_form(self) -> None:
        """user_input=None → show form."""
        flow, _ = _make_flow_instance()
        result = await flow.async_step_confirm(user_input=None)
        flow.async_show_form.assert_called_once()
        assert result == {"type": "form"}

    @pytest.mark.asyncio
    async def test_confirm_acknowledge_creates_entry(self) -> None:
        """user_input with action=acknowledge → create entry without min_soc change."""
        flow, entry = _make_flow_instance()
        await flow.async_step_confirm(user_input={"action": "acknowledge"})
        flow.async_create_entry.assert_called_once_with(data={})
        # min_soc NOT updated
        flow.hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_increase_min_soc_updates_and_creates(self) -> None:
        """user_input with action=increase_min_soc → update entry + create entry."""
        flow, entry = _make_flow_instance()
        entry.options = {"min_soc": 15.0}
        await flow.async_step_confirm(user_input={"action": "increase_min_soc"})
        # min_soc should be increased by 5
        flow.hass.config_entries.async_update_entry.assert_called_once()
        new_opts = flow.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert new_opts["min_soc"] == 20.0
        flow.async_create_entry.assert_called_once_with(data={})


# ── Tests: _increase_min_soc ──────────────────────────────────────────────────


class TestIncreaseMinSoc:
    """Lines 69-77: _increase_min_soc with entries and without."""

    @pytest.mark.asyncio
    async def test_no_entries_returns_early(self) -> None:
        """Empty entries → no update_entry call."""
        flow, _ = _make_flow_instance()
        flow.hass.config_entries.async_entries.return_value = []
        await flow._increase_min_soc()
        flow.hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_increases_soc_by_5_percent(self) -> None:
        """min_soc=20 → updated to 25."""
        flow, entry = _make_flow_instance()
        entry.options = {"min_soc": 20.0}
        await flow._increase_min_soc()
        new_opts = flow.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert new_opts["min_soc"] == 25.0

    @pytest.mark.asyncio
    async def test_clamps_at_50_percent(self) -> None:
        """min_soc=48 → clamped to 50 (not 53)."""
        flow, entry = _make_flow_instance()
        entry.options = {"min_soc": 48.0}
        await flow._increase_min_soc()
        new_opts = flow.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert new_opts["min_soc"] == 50.0

    @pytest.mark.asyncio
    async def test_default_soc_when_not_in_options(self) -> None:
        """options without min_soc → uses DEFAULT_BATTERY_MIN_SOC as base."""
        from custom_components.carmabox.const import DEFAULT_BATTERY_MIN_SOC

        flow, entry = _make_flow_instance()
        entry.options = {}
        await flow._increase_min_soc()
        new_opts = flow.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert new_opts["min_soc"] == DEFAULT_BATTERY_MIN_SOC + 5.0


# ── Tests: _get_placeholders ──────────────────────────────────────────────────


class TestGetPlaceholders:
    """Lines 81-85: _get_placeholders with/without entries."""

    def test_with_entry_returns_soc_value(self) -> None:
        flow, entry = _make_flow_instance()
        entry.options = {"min_soc": 30.0}
        result = flow._get_placeholders()
        assert result["current_min_soc"] == "30"

    def test_without_entries_uses_default(self) -> None:
        from custom_components.carmabox.const import DEFAULT_BATTERY_MIN_SOC

        flow, _ = _make_flow_instance()
        flow.hass.config_entries.async_entries.return_value = []
        result = flow._get_placeholders()
        assert result["current_min_soc"] == f"{DEFAULT_BATTERY_MIN_SOC:.0f}"


# ── Tests: async_create_fix_flow ──────────────────────────────────────────────


class TestAsyncCreateFixFlow:
    """Lines 96-101: async_create_fix_flow issue routing."""

    @pytest.mark.asyncio
    async def test_safety_guard_issue_returns_safety_flow(self) -> None:
        from custom_components.carmabox.repairs import (
            SafetyGuardRepairFlow,
            async_create_fix_flow,
        )

        hass = MagicMock()
        flow = await async_create_fix_flow(hass, "safety_guard_frequent_blocks", {})
        assert isinstance(flow, SafetyGuardRepairFlow)

    @pytest.mark.asyncio
    async def test_other_issue_returns_hub_offline_flow(self) -> None:
        from custom_components.carmabox.repairs import (
            HubOfflineRepairFlow,
            async_create_fix_flow,
        )

        hass = MagicMock()
        flow = await async_create_fix_flow(hass, "hub_offline", {})
        assert isinstance(flow, HubOfflineRepairFlow)
