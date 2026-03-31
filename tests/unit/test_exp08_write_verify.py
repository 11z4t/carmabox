"""EXP-08: Write verification tests for GoodWe EMS mode.

Tests the verify=True path in set_ems_mode:
- Successful verify on first read
- Verify mismatch → retry → success
- Verify mismatch → retry → permanent failure (returns False)
- No verify (default) → skip read-back
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.carmabox.adapters.goodwe import _VERIFY_DELAY_S


def test_verify_delay_constant():
    """_VERIFY_DELAY_S should be a positive number."""
    assert _VERIFY_DELAY_S > 0
    assert _VERIFY_DELAY_S <= 5.0  # Sanity: never wait more than 5s


@pytest.fixture
def mock_adapter():
    """Create a minimal mock GoodWe adapter for verify testing."""
    from custom_components.carmabox.adapters.goodwe import GoodWeAdapter

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(return_value=None)
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)

    adapter = GoodWeAdapter.__new__(GoodWeAdapter)
    adapter.hass = hass
    adapter.prefix = "kontor"
    adapter._entry_id = "test_entry"
    adapter._device_id = "test_device"
    adapter._analyze_only = False
    return adapter


@pytest.mark.asyncio
async def test_verify_success_first_read(mock_adapter):
    """verify=True, read-back matches → return True."""
    mock_adapter._safe_call = AsyncMock(return_value=True)

    with patch.object(type(mock_adapter), "ems_mode", new_callable=PropertyMock) as mock_ems:
        mock_ems.return_value = "charge_pv"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mock_adapter.set_ems_mode("charge_pv", verify=True)

    assert result is True


@pytest.mark.asyncio
async def test_verify_mismatch_retry_success(mock_adapter):
    """verify=True, first read mismatches, retry succeeds."""

    async def mock_safe_call(*args, **kwargs):
        return True

    mock_adapter._safe_call = mock_safe_call

    reads = iter(["battery_standby", "charge_pv"])

    with patch.object(type(mock_adapter), "ems_mode", new_callable=PropertyMock) as mock_ems:
        mock_ems.side_effect = lambda: next(reads)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mock_adapter.set_ems_mode("charge_pv", verify=True)

    assert result is True


@pytest.mark.asyncio
async def test_verify_permanent_failure(mock_adapter):
    """verify=True, both reads mismatch → return False (permanent failure)."""
    mock_adapter._safe_call = AsyncMock(return_value=True)

    with patch.object(type(mock_adapter), "ems_mode", new_callable=PropertyMock) as mock_ems:
        # Always returns wrong mode
        mock_ems.return_value = "battery_standby"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mock_adapter.set_ems_mode("charge_pv", verify=True)

    assert result is False


@pytest.mark.asyncio
async def test_no_verify_skips_readback(mock_adapter):
    """verify=False (default) → no sleep, no read-back."""
    mock_adapter._safe_call = AsyncMock(return_value=True)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await mock_adapter.set_ems_mode("charge_pv", verify=False)

    assert result is True
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_mode_rejected(mock_adapter):
    """Invalid EMS mode → return False, never call service."""
    mock_adapter._safe_call = AsyncMock()
    result = await mock_adapter.set_ems_mode("invalid_mode", verify=True)
    assert result is False
    mock_adapter._safe_call.assert_not_called()


# ── EXP-EPIC-SWEEP edge cases ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_call_fails_skips_verify(mock_adapter):
    """EXP-08 edge: _safe_call returns False → return False without verifying.

    If the write itself failed, reading back the mode is pointless.
    Verify sleep should NOT be called.
    """
    mock_adapter._safe_call = AsyncMock(return_value=False)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await mock_adapter.set_ems_mode("charge_pv", verify=True)

    assert result is False
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_verify_with_discharge_pv_mode(mock_adapter):
    """EXP-08 edge: verify works for all valid modes, not just charge_pv."""
    mock_adapter._safe_call = AsyncMock(return_value=True)

    from unittest.mock import PropertyMock, patch

    with patch.object(type(mock_adapter), "ems_mode", new_callable=PropertyMock) as mock_ems:
        mock_ems.return_value = "discharge_pv"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mock_adapter.set_ems_mode("discharge_pv", verify=True)

    assert result is True


@pytest.mark.asyncio
async def test_verify_retry_write_succeeds_but_second_verify_fails(mock_adapter):
    """EXP-08 edge: both writes succeed but second verify still fails → return False.

    Write OK → verify fails → retry write OK → second verify still fails → False.
    This is the permanent failure path (e.g., Modbus register not accepting value).
    """
    from unittest.mock import PropertyMock, patch

    mock_adapter._safe_call = AsyncMock(return_value=True)

    with patch.object(type(mock_adapter), "ems_mode", new_callable=PropertyMock) as mock_ems:
        mock_ems.return_value = "battery_standby"  # Both verifications fail
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mock_adapter.set_ems_mode("charge_pv", verify=True)

    assert result is False  # Both verifications failed → permanent failure
