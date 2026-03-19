"""Tests for repairs platform."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.carmabox.repairs import (
    SAFETY_BLOCK_THRESHOLD,
    clear_issue,
    raise_hub_offline_issue,
    raise_safety_guard_issue,
)


class TestSafetyGuardIssue:
    def test_raise_issue(self) -> None:
        hass = MagicMock()
        with patch("custom_components.carmabox.repairs.ir.async_create_issue") as mock_create:
            raise_safety_guard_issue(hass, 15)
            mock_create.assert_called_once()

    def test_threshold_value(self) -> None:
        assert SAFETY_BLOCK_THRESHOLD > 0


class TestHubOfflineIssue:
    def test_raise_issue(self) -> None:
        hass = MagicMock()
        with patch("custom_components.carmabox.repairs.ir.async_create_issue") as mock_create:
            raise_hub_offline_issue(hass, 48)
            mock_create.assert_called_once()


class TestClearIssue:
    def test_clear(self) -> None:
        hass = MagicMock()
        with patch("custom_components.carmabox.repairs.ir.async_delete_issue") as mock_delete:
            clear_issue(hass, "safety_guard_frequent_blocks")
            mock_delete.assert_called_once()
