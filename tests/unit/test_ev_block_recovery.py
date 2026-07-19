"""EXP-05: reason_for_no_current monitoring + auto-recovery — coordinator tests.

Covers coordinator._handle_ev_block_recovery:
  - Per-cycle logging of reason_for_no_current while EV is plugged in.
  - Recovery dispatch for each handled reason code (51 WaitingInFully,
    6 MaxCircuitCurrentTooLow).
  - Backoff/max-retry guardrail so a persistently blocked charger cannot
    trigger an unbounded retry loop against the Easee cloud API.

Uses the same lightweight coordinator factory as the other EXP-* control
story tests (see test_coordinator_ev_control._make_ev_coord).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.carmabox.const import (
    EV_RECOVERY_COOLDOWN_S,
    EV_RECOVERY_MAX_ATTEMPTS,
)
from tests.unit.test_coordinator_ev_control import _make_ev_coord


def _wired_ev_adapter(
    *,
    reason: str = "",
    needs_recovery: bool = False,
    recovery_result: str | None = None,
) -> MagicMock:
    ev = MagicMock()
    ev.reason_for_no_current = reason
    ev.needs_recovery = needs_recovery
    ev.try_recover = AsyncMock(return_value=recovery_result)
    return ev


class TestNoAdapterOrDisconnected:
    """Guardrail: nothing happens without an adapter or an unplugged EV."""

    @pytest.mark.asyncio
    async def test_no_adapter_is_noop(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = None
        await coord._handle_ev_block_recovery(ev_connected=True)
        # Must not raise — no adapter to query

    @pytest.mark.asyncio
    async def test_ev_not_connected_does_not_query_reason(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(reason="51", needs_recovery=True)
        await coord._handle_ev_block_recovery(ev_connected=False)
        coord.ev_adapter.try_recover.assert_not_called()


class TestPerCycleLogging:
    """AC: coordinator logs/exposes reason_for_no_current every cycle while plugged in."""

    @pytest.mark.asyncio
    async def test_reason_stored_even_when_no_recovery_needed(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(reason="", needs_recovery=False)
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord._ev_block_reason == ""
        coord.ev_adapter.try_recover.assert_not_called()

    @pytest.mark.asyncio
    async def test_reason_stored_when_blocked(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord._ev_block_reason == "51"


class TestRecoveryPerReasonCode:
    """One test per Easee reason code CARMA Box actively recovers from."""

    @pytest.mark.asyncio
    async def test_reason_51_waiting_in_fully_triggers_recovery(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        coord.ev_adapter.try_recover.assert_awaited_once()
        assert coord._ev_recovery_attempts == 1

    @pytest.mark.asyncio
    async def test_reason_6_max_circuit_current_too_low_triggers_recovery(self) -> None:
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="6", needs_recovery=True, recovery_result="circuit_low_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        coord.ev_adapter.try_recover.assert_awaited_once()
        assert coord._ev_recovery_attempts == 1

    @pytest.mark.asyncio
    async def test_recovery_clears_after_reason_resolves(self) -> None:
        """Once the adapter reports needs_recovery=False, backoff state resets."""
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord._ev_recovery_attempts == 1

        # Reason resolved on a later cycle
        coord.ev_adapter.reason_for_no_current = ""
        coord.ev_adapter.needs_recovery = False
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord._ev_recovery_attempts == 0
        assert coord._ev_recovery_last_reason is None


class TestRecoveryBackoff:
    """Prevents an infinite retry loop writing to Easee cloud every cycle."""

    @pytest.mark.asyncio
    async def test_second_call_within_retry_interval_is_skipped(self) -> None:
        """Two cycles in a row (no time elapsed) → only ONE try_recover call."""
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        await coord._handle_ev_block_recovery(ev_connected=True)

        assert coord.ev_adapter.try_recover.await_count == 1
        assert coord._ev_recovery_attempts == 1

    @pytest.mark.asyncio
    async def test_retry_allowed_after_interval_elapses(self) -> None:
        """Once EV_RECOVERY_RETRY_INTERVAL_S has passed, a second attempt is allowed."""
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord.ev_adapter.try_recover.await_count == 1

        # Simulate the retry-interval backoff window having elapsed
        coord._ev_recovery_next_attempt_at = time.monotonic() - 1

        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord.ev_adapter.try_recover.await_count == 2
        assert coord._ev_recovery_attempts == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts_then_cools_down(self) -> None:
        """After EV_RECOVERY_MAX_ATTEMPTS, recovery pauses instead of retrying forever."""
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )

        for _ in range(EV_RECOVERY_MAX_ATTEMPTS):
            await coord._handle_ev_block_recovery(ev_connected=True)
            # Force the retry-interval window open for the next attempt
            coord._ev_recovery_next_attempt_at = time.monotonic() - 1

        assert coord.ev_adapter.try_recover.await_count == EV_RECOVERY_MAX_ATTEMPTS

        # One more cycle: attempts exhausted → NO further try_recover call this cycle
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord.ev_adapter.try_recover.await_count == EV_RECOVERY_MAX_ATTEMPTS

        # Cooldown window must be roughly EV_RECOVERY_COOLDOWN_S out
        remaining = coord._ev_recovery_next_attempt_at - time.monotonic()
        assert remaining > EV_RECOVERY_COOLDOWN_S - 5

    @pytest.mark.asyncio
    async def test_reason_change_resets_backoff_counter(self) -> None:
        """A new/different block reason is treated as a fresh problem — no leftover backoff."""
        coord = _make_ev_coord()
        coord.ev_adapter = _wired_ev_adapter(
            reason="51", needs_recovery=True, recovery_result="waiting_in_fully_fix"
        )
        await coord._handle_ev_block_recovery(ev_connected=True)
        assert coord._ev_recovery_attempts == 1
        # Still within backoff window for reason 51 — would normally be skipped
        assert coord._ev_recovery_next_attempt_at > time.monotonic()

        # Reason changes to 6 on the next cycle (new problem)
        coord.ev_adapter.reason_for_no_current = "6"
        coord.ev_adapter.try_recover = AsyncMock(return_value="circuit_low_fix")
        await coord._handle_ev_block_recovery(ev_connected=True)

        coord.ev_adapter.try_recover.assert_awaited_once()
        assert coord._ev_recovery_attempts == 1
        assert coord._ev_recovery_last_reason == "6"
