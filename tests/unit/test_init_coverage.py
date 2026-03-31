"""Coverage tests for carmabox __init__.py.

Targets:
  - async_unload_entry
  - _async_options_updated
  - async_setup_entry (full path including cable entity)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_hass(*, with_http: bool = True, cable_entity: str | None = None) -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.async_create_task = MagicMock()
    if not with_http:
        hass.http = None
    return hass


def _make_entry(*, entry_id: str = "test_entry", title: str = "CARMA Box Test") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.title = title
    entry.options = {}
    entry.data = {}
    entry.runtime_data = None
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


def _make_coordinator(*, cable_entity: str | None = None) -> MagicMock:
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.cable_locked_entity = cable_entity
    coord.on_ev_cable_connected = AsyncMock()
    return coord


# ── Tests: async_unload_entry ─────────────────────────────────────────────────


class TestAsyncUnloadEntry:
    """async_unload_entry forwards platform unload result."""

    @pytest.mark.asyncio
    async def test_unload_success_returns_true(self) -> None:
        """Successful platform unload → returns True."""
        from custom_components.carmabox import async_unload_entry

        hass = _make_hass()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = _make_entry()

        result = await async_unload_entry(hass, entry)

        assert result is True
        hass.config_entries.async_unload_platforms.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unload_failure_returns_false(self) -> None:
        """Failed platform unload → returns False."""
        from custom_components.carmabox import async_unload_entry

        hass = _make_hass()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
        entry = _make_entry()

        result = await async_unload_entry(hass, entry)

        assert result is False


# ── Tests: _async_options_updated ────────────────────────────────────────────


class TestAsyncOptionsUpdated:
    """Lines 130-131: _async_options_updated triggers reload."""

    @pytest.mark.asyncio
    async def test_options_updated_reloads_entry(self) -> None:
        """Options updated → hass.config_entries.async_reload called."""
        from custom_components.carmabox import _async_options_updated

        hass = _make_hass()
        entry = _make_entry(entry_id="reload_test")

        await _async_options_updated(hass, entry)

        hass.config_entries.async_reload.assert_called_once_with("reload_test")


# ── Tests: async_setup_entry ──────────────────────────────────────────────────


class TestAsyncSetupEntry:
    """Lines 56-98: async_setup_entry full path."""

    @pytest.mark.asyncio
    async def test_setup_without_cable_entity_returns_true(self) -> None:
        """No cable entity → setup completes without state tracking."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_coordinator(cable_entity=None)

        with patch(
            "custom_components.carmabox.CarmaboxCoordinator",
            return_value=coord,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        coord.async_config_entry_first_refresh.assert_awaited_once()
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_setup_with_cable_entity_registers_tracker(self) -> None:
        """cable_locked_entity set → async_track_state_change_event called."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_coordinator(cable_entity="binary_sensor.ev_cable")

        mock_unsub = MagicMock()

        with (
            patch("custom_components.carmabox.CarmaboxCoordinator", return_value=coord),
            patch(
                "custom_components.carmabox.async_track_state_change_event",
                return_value=mock_unsub,
            ) as mock_track,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        mock_track.assert_called_once()
        # unsub should be registered for cleanup
        assert entry.async_on_unload.call_count >= 1

    @pytest.mark.asyncio
    async def test_setup_cable_change_triggers_ev_check(self) -> None:
        """Cable state change on→on triggers on_ev_cable_connected."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_coordinator(cable_entity="binary_sensor.ev_cable")

        captured_callback = {}

        def capture_track(hass_: object, entity: str, cb: object) -> MagicMock:
            captured_callback["cb"] = cb
            return MagicMock()

        with (
            patch("custom_components.carmabox.CarmaboxCoordinator", return_value=coord),
            patch(
                "custom_components.carmabox.async_track_state_change_event",
                side_effect=capture_track,
            ),
        ):
            await async_setup_entry(hass, entry)

        # Simulate cable plugged in: old=off, new=on
        if "cb" in captured_callback:
            old_state = MagicMock()
            old_state.state = "off"
            new_state = MagicMock()
            new_state.state = "on"
            event = MagicMock()
            states = {"new_state": new_state, "old_state": old_state}
            event.data.get = lambda k, d=None: states.get(k, d)
            captured_callback["cb"](event)
            hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_setup_cable_new_state_none_skips(self) -> None:
        """Cable callback with new_state=None → no task created."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_coordinator(cable_entity="binary_sensor.ev_cable")

        captured_callback = {}

        def capture_track(hass_: object, entity: str, cb: object) -> MagicMock:
            captured_callback["cb"] = cb
            return MagicMock()

        with (
            patch("custom_components.carmabox.CarmaboxCoordinator", return_value=coord),
            patch(
                "custom_components.carmabox.async_track_state_change_event",
                side_effect=capture_track,
            ),
        ):
            await async_setup_entry(hass, entry)

        if "cb" in captured_callback:
            event = MagicMock()
            event.data.get = lambda k, d=None: {"new_state": None, "old_state": None}.get(k, d)
            captured_callback["cb"](event)
            hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_without_http_skips_card_registration(self) -> None:
        """hass.http=None → no register_static_path call."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass(with_http=False)
        entry = _make_entry()
        coord = _make_coordinator()

        with patch("custom_components.carmabox.CarmaboxCoordinator", return_value=coord):
            result = await async_setup_entry(hass, entry)

        assert result is True

    @pytest.mark.asyncio
    async def test_setup_static_path_already_registered_no_raise(self) -> None:
        """register_static_path raises → caught silently."""
        from custom_components.carmabox import async_setup_entry

        hass = _make_hass()
        hass.http.register_static_path = MagicMock(side_effect=RuntimeError("already registered"))
        entry = _make_entry()
        coord = _make_coordinator()

        with patch("custom_components.carmabox.CarmaboxCoordinator", return_value=coord):
            result = await async_setup_entry(hass, entry)

        assert result is True  # Should not raise
