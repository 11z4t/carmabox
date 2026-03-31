"""ML-01: Verify predictor save/restore via _async_save_predictor/_async_restore_predictor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.optimizer.predictor import ConsumptionPredictor, HourSample


def _make_coordinator_with_predictor(predictor: ConsumptionPredictor):
    """Create a minimal coordinator stub with a predictor and mocked store."""
    from custom_components.carmabox.coordinator import CarmaboxCoordinator

    coord = CarmaboxCoordinator.__new__(CarmaboxCoordinator)
    coord.predictor = predictor
    coord._predictor_last_save = 0.0
    coord._predictor_store = MagicMock()
    coord._predictor_store.async_save = AsyncMock()
    coord._predictor_store.async_load = AsyncMock()
    return coord


class TestAsyncSavePredictor:
    @pytest.mark.asyncio
    async def test_save_calls_store_with_dict(self):
        p = ConsumptionPredictor()
        p.add_sample(HourSample(weekday=0, hour=8, month=3, consumption_kw=2.0))
        coord = _make_coordinator_with_predictor(p)

        await coord._async_save_predictor()

        coord._predictor_store.async_save.assert_called_once()
        saved = coord._predictor_store.async_save.call_args[0][0]
        assert saved["total_samples"] == 1
        assert "history" in saved
        assert "seasonal_factor" in saved

    @pytest.mark.asyncio
    async def test_save_rate_limited(self):
        """Second call within interval should be skipped."""
        import time

        p = ConsumptionPredictor()
        coord = _make_coordinator_with_predictor(p)
        coord._predictor_last_save = time.monotonic()  # Already saved just now

        await coord._async_save_predictor()

        coord._predictor_store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_survives_store_exception(self):
        p = ConsumptionPredictor()
        coord = _make_coordinator_with_predictor(p)
        coord._predictor_store.async_save = AsyncMock(side_effect=OSError("disk full"))

        # Should not raise
        await coord._async_save_predictor()


class TestAsyncRestorePredictor:
    @pytest.mark.asyncio
    async def test_restore_loads_samples(self):
        """Restore reconstructs predictor state from stored dict."""
        original = ConsumptionPredictor()
        for wd in range(7):
            for h in range(24):
                original.add_sample(HourSample(weekday=wd, hour=h, month=3, consumption_kw=2.5))
        stored = original.to_dict()

        coord = _make_coordinator_with_predictor(ConsumptionPredictor())
        coord._predictor_store.async_load = AsyncMock(return_value=stored)

        await coord._async_restore_predictor()

        assert coord.predictor.total_samples == original.total_samples
        assert coord.predictor.history == original.history

    @pytest.mark.asyncio
    async def test_restore_empty_store_leaves_fresh_predictor(self):
        coord = _make_coordinator_with_predictor(ConsumptionPredictor())
        coord._predictor_store.async_load = AsyncMock(return_value=None)

        await coord._async_restore_predictor()

        assert coord.predictor.total_samples == 0

    @pytest.mark.asyncio
    async def test_restore_survives_store_exception(self):
        coord = _make_coordinator_with_predictor(ConsumptionPredictor())
        coord._predictor_store.async_load = AsyncMock(side_effect=OSError("corrupt"))

        # Should not raise — fresh predictor stays
        await coord._async_restore_predictor()

        assert coord.predictor.total_samples == 0

    @pytest.mark.asyncio
    async def test_restore_roundtrip(self):
        """to_dict → from_dict round-trip preserves all fields."""
        original = ConsumptionPredictor()
        original.add_sample(HourSample(weekday=1, hour=14, month=6, consumption_kw=3.1))
        original.seasonal_factor[7] = 0.65  # Custom factor

        coord = _make_coordinator_with_predictor(ConsumptionPredictor())
        coord._predictor_store.async_load = AsyncMock(return_value=original.to_dict())

        await coord._async_restore_predictor()

        assert coord.predictor.total_samples == 1
        assert coord.predictor.history["1_14"] == [3.1]
        assert coord.predictor.seasonal_factor[7] == pytest.approx(0.65)
