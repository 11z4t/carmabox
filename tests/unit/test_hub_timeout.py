"""Tests for hub.py timeout configuration (PLAT-1214)."""

from __future__ import annotations

import aiohttp

from custom_components.carmabox.const import HUB_SYNC_TIMEOUT_S
from custom_components.carmabox.hub import HubSyncClient


class TestHubTimeout:
    def test_hub_sync_timeout_is_10s(self) -> None:
        """HUB_SYNC_TIMEOUT_S must be 10 — HA manifest requires ≤10s."""
        assert HUB_SYNC_TIMEOUT_S == 10

    def test_hub_uses_const_not_magic_number(self) -> None:
        """Verify hub.py no longer has a local SYNC_TIMEOUT constant."""
        import custom_components.carmabox.hub as hub_module

        assert not hasattr(hub_module, "SYNC_TIMEOUT"), (
            "SYNC_TIMEOUT should have been removed from hub.py — use HUB_SYNC_TIMEOUT_S from const.py"
        )

    def test_aiohttp_timeout_uses_hub_const(self) -> None:
        """Confirm the constant value is usable as aiohttp timeout."""
        timeout = aiohttp.ClientTimeout(total=HUB_SYNC_TIMEOUT_S)
        assert timeout.total == HUB_SYNC_TIMEOUT_S
