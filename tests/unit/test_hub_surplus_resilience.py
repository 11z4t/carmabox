"""Coverage tests — batch 16.

Targets:
  hub.py:              289-298, 301-302, 305-333  (actual connect_mqtt closures)
  core/resilience.py:  85, 111-112, 145, 163, 188, 214, 217
  core/surplus_chain.py: 237, 246, 265, 360, 449, 451, 504-505,
                          522-526, 539-542, 633
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

pytest.importorskip("paho", reason="paho-mqtt not installed")

# ══════════════════════════════════════════════════════════════════════════════
# hub.py — actual connect_mqtt closures
# ══════════════════════════════════════════════════════════════════════════════


def _make_hub(*, hmac_key: str = "") -> object:
    from custom_components.carmabox.hub import HubSyncClient

    hass = MagicMock()
    return HubSyncClient(
        hass=hass,
        instance_id="test-box",
        mqtt_username="user1",
        mqtt_token="secret",
        mqtt_hmac_key=hmac_key,
    )


async def _connect_and_get_callbacks(hub: object) -> tuple[object, object, object, object]:
    """Call connect_mqtt() with mocked paho, return (mock_client, on_connect,
    on_disconnect, on_message)."""
    mock_client = MagicMock()

    with patch("paho.mqtt.client.Client", return_value=mock_client):
        await hub.connect_mqtt()  # type: ignore[union-attr]

    return (
        mock_client,
        mock_client.on_connect,
        mock_client.on_disconnect,
        mock_client.on_message,
    )


class TestHubConnectMQTTCallbacks:
    """Lines 289-298, 301-302, 305-333."""

    @pytest.mark.asyncio
    async def test_on_connect_rc0_connected_and_subscribes(self) -> None:
        """rc=0 → _mqtt_connected=True, subscribes 3 topics (lines 290-296)."""
        hub = _make_hub()
        mock_client, on_connect, _, _ = await _connect_and_get_callbacks(hub)
        on_connect(mock_client, None, None, 0)
        assert hub._mqtt_connected is True  # type: ignore[union-attr]
        assert mock_client.subscribe.call_count == 3

    @pytest.mark.asyncio
    async def test_on_connect_rc_nonzero_sets_disconnected(self) -> None:
        """rc!=0 → _mqtt_connected=False (lines 297-298)."""
        hub = _make_hub()
        hub._mqtt_connected = True  # type: ignore[union-attr]
        mock_client, on_connect, _, _ = await _connect_and_get_callbacks(hub)
        on_connect(mock_client, None, None, 5)
        assert hub._mqtt_connected is False  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_on_disconnect_clears_connected(self) -> None:
        """on_disconnect → _mqtt_connected=False (lines 301-302)."""
        hub = _make_hub()
        hub._mqtt_connected = True  # type: ignore[union-attr]
        mock_client, _, on_disconnect, _ = await _connect_and_get_callbacks(hub)
        on_disconnect(mock_client, None, 0)
        assert hub._mqtt_connected is False  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_on_message_json_error_returns_early(self) -> None:
        """Bad JSON → return early, no callback called (lines 308-310)."""
        hub = _make_hub()
        received = []
        hub._on_config_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/config"  # type: ignore[union-attr]
        msg.payload = b"bad json{"
        on_message(mock_client, None, msg)
        assert received == []

    @pytest.mark.asyncio
    async def test_on_message_config_callback_called(self) -> None:
        """Valid JSON on /config → _on_config_callback invoked (line 327)."""
        hub = _make_hub()
        received = []
        hub._on_config_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/config"  # type: ignore[union-attr]
        msg.payload = json.dumps({"ems_mode": "peak_shaving"}).encode()
        on_message(mock_client, None, msg)
        assert received == [{"ems_mode": "peak_shaving"}]

    @pytest.mark.asyncio
    async def test_on_message_command_callback_called(self) -> None:
        """Valid JSON on /command → _on_command_callback invoked (line 329)."""
        hub = _make_hub()
        received = []
        hub._on_command_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/command"  # type: ignore[union-attr]
        msg.payload = json.dumps({"action": "replan"}).encode()
        on_message(mock_client, None, msg)
        assert received == [{"action": "replan"}]

    @pytest.mark.asyncio
    async def test_on_message_insights_logs_keys(self) -> None:
        """Valid JSON on /insights → logged, no callback (line 331-333)."""
        hub = _make_hub()
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/insights"  # type: ignore[union-attr]
        msg.payload = json.dumps({"score": 0.9, "tip": "lower usage"}).encode()
        # Should not raise
        on_message(mock_client, None, msg)

    @pytest.mark.asyncio
    async def test_on_message_hmac_invalid_drops(self) -> None:
        """HMAC key set, bad sig → drop (lines 317-321)."""
        hub = _make_hub(hmac_key="secret-key")
        received = []
        hub._on_config_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/config"  # type: ignore[union-attr]
        msg.payload = json.dumps({"sig": "bad", "payload": {}, "ts": "1", "nonce": "abc"}).encode()
        on_message(mock_client, None, msg)
        assert received == []

    @pytest.mark.asyncio
    async def test_on_message_hmac_valid_unwraps_payload(self) -> None:
        """HMAC key set + valid sig → payload=unwrapped passed to callback (line 321)."""
        from custom_components.carmabox.hub import _sign_mqtt_payload

        hmac_key = "test-secret"
        hub = _make_hub(hmac_key=hmac_key)
        received = []
        hub._on_config_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)

        inner_payload = {"ems_mode": "peak_shaving"}
        envelope = _sign_mqtt_payload(inner_payload, hmac_key)  # Valid HMAC

        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/config"  # type: ignore[union-attr]
        msg.payload = json.dumps(envelope).encode()
        on_message(mock_client, None, msg)
        # Callback receives the inner payload, not the envelope
        assert received == [inner_payload]

    @pytest.mark.asyncio
    async def test_on_message_hmac_unsigned_accepted(self) -> None:
        """HMAC key set but no sig → accept with debug (lines 322-325)."""
        hub = _make_hub(hmac_key="secret-key")
        received = []
        hub._on_config_callback = lambda p: received.append(p)  # type: ignore[union-attr]
        mock_client, _, _, on_message = await _connect_and_get_callbacks(hub)
        msg = MagicMock()
        msg.topic = f"{hub.topic_prefix}/config"  # type: ignore[union-attr]
        msg.payload = json.dumps({"ems_mode": "standby"}).encode()  # No "sig" key
        on_message(mock_client, None, msg)
        assert received == [{"ems_mode": "standby"}]


# ══════════════════════════════════════════════════════════════════════════════
# core/resilience.py — remaining gaps
# ══════════════════════════════════════════════════════════════════════════════


def _make_mgr() -> object:
    from custom_components.carmabox.core.resilience import ResilienceManager

    return ResilienceManager()


class TestResilienceRemainingGaps:
    """Lines 85, 111-112, 145, 163, 188, 214, 217."""

    def test_update_sensor_auto_registers(self) -> None:
        """update_sensor on unknown entity → auto-register (line 85)."""
        mgr = _make_mgr()
        mgr.update_sensor("sensor.new", 5.0)  # type: ignore[union-attr]
        assert "sensor.new" in mgr._fallbacks  # type: ignore[union-attr]
        assert mgr._fallbacks["sensor.new"].last_known == 5.0  # type: ignore[union-attr]

    def test_get_value_non_float_current_triggers_except(self) -> None:
        """Non-float current → TypeError in _is_unavailable → pass (lines 111-112)."""
        mgr = _make_mgr()
        mgr.register_sensor("sensor.test")  # type: ignore[union-attr]
        # Pass a string: math.isnan("x") raises TypeError
        _value, is_fb = mgr.get_value("sensor.test", current="unavailable")  # type: ignore[union-attr]
        # Falls back to default (0.0) since no last_update
        assert is_fb is True

    def test_record_success_directly_clears_trip(self) -> None:
        """record_success when cb.tripped=True → clears it (line 145)."""
        mgr = _make_mgr()
        mgr.register_breaker("adapter1", max_errors=2)  # type: ignore[union-attr]
        mgr.record_error("adapter1")  # type: ignore[union-attr]
        mgr.record_error("adapter1")  # trips (max_errors=2)
        # cb.tripped is True — now call record_success DIRECTLY (no is_breaker_open)
        assert mgr._circuit_breakers["adapter1"].tripped is True  # type: ignore[union-attr]
        mgr.record_success("adapter1")  # type: ignore[union-attr]
        assert mgr._circuit_breakers["adapter1"].tripped is False  # type: ignore[union-attr]

    def test_is_breaker_open_unregistered_returns_false(self) -> None:
        """Unregistered adapter → cb=None → return False (line 163)."""
        mgr = _make_mgr()
        result = mgr.is_breaker_open("never_registered")  # type: ignore[union-attr]
        assert result is False

    def test_get_rate_usage_returns_current_and_max(self) -> None:
        """get_rate_usage() returns (used, max) tuple (line 188)."""
        mgr = _make_mgr()
        used, max_per_hour = mgr.get_rate_usage()  # type: ignore[union-attr]
        assert used == 0
        assert max_per_hour > 0

    def test_status_sensor_fallback_level_1(self) -> None:
        """Stale sensor → degraded_level=1 → status includes 'sensor' (line 214)."""
        mgr = _make_mgr()
        mgr.register_sensor("sensor.stale")  # type: ignore[union-attr]
        mgr._fallbacks["sensor.stale"].max_age_s = 1  # type: ignore[union-attr]
        mgr.update_sensor("sensor.stale", 1.0, ts=time.monotonic() - 100)  # type: ignore[union-attr]
        status = mgr.status  # type: ignore[union-attr]
        assert "sensor" in status.lower()

    def test_status_level_above_2_fallback_string(self) -> None:
        """degraded_level > 2 → generic 'Degraderad: nivå' string (line 217)."""
        mgr = _make_mgr()
        with patch.object(type(mgr), "degraded_level", new_callable=PropertyMock, return_value=3):
            status = mgr.status  # type: ignore[union-attr]
        assert "Degraderad" in status


# ══════════════════════════════════════════════════════════════════════════════
# core/surplus_chain.py
# ══════════════════════════════════════════════════════════════════════════════


def _c(
    cid: str,
    *,
    priority: int = 5,
    ctype: str = "on_off",
    min_w: float = 0.0,
    max_w: float = 1000.0,
    current_w: float = 0.0,
    is_running: bool = False,
    phase_count: int = 1,
    dependency_met: bool = True,
) -> object:
    """Helper: build SurplusConsumer."""
    from custom_components.carmabox.core.surplus_chain import ConsumerType, SurplusConsumer

    ct = ConsumerType.VARIABLE if ctype == "variable" else ConsumerType.ON_OFF
    return SurplusConsumer(
        id=cid,
        name=cid,
        priority=priority,
        type=ct,
        min_w=min_w,
        max_w=max_w,
        current_w=current_w,
        is_running=is_running,
        phase_count=phase_count,
        dependency_met=dependency_met,
    )


class TestSurplusChainGaps:
    """Lines 237, 246, 265, 360, 449, 451, 504-505, 522-526, 539-542, 633."""

    def test_pass1_variable_at_max_skipped(self) -> None:
        """VARIABLE running at max_w → continue in Pass 1 (line 237)."""
        from custom_components.carmabox.core.surplus_chain import (
            SurplusConfig,
            allocate_surplus,
        )

        # Consumer is VARIABLE, running, at max — should be skipped in pass 1
        consumer = _c(
            "ev", ctype="variable", min_w=500, max_w=1000, current_w=1000, is_running=True
        )
        cfg = SurplusConfig(start_delay_s=0)
        result = allocate_surplus(5000.0, [consumer], config=cfg)  # type: ignore[arg-type]
        # No increase action since already at max
        ev_alloc = next(a for a in result.allocations if a.id == "ev")
        assert ev_alloc.action != "increase"

    def test_pass1_3phase_zero_steps_skipped(self) -> None:
        """3-phase EV, surplus too small for 1 step → continue (line 246)."""
        from custom_components.carmabox.core.surplus_chain import (
            SurplusConfig,
            allocate_surplus,
        )

        # w_per_step = 230 * 3 = 690W; with surplus 100W steps=0 → skip
        consumer = _c(
            "ev",
            ctype="variable",
            min_w=500,
            max_w=5000,
            current_w=1000,
            is_running=True,
            phase_count=3,
        )
        cfg = SurplusConfig(min_surplus_w=50, start_delay_s=0)
        result = allocate_surplus(100.0, [consumer], config=cfg)  # type: ignore[arg-type]
        ev_alloc = next(a for a in result.allocations if a.id == "ev")
        assert ev_alloc.action != "increase"

    def test_pass1_tiny_increase_gets_none_action(self) -> None:
        """headroom < 50W → increase ≤ 50 → 'none' allocation (line 265)."""
        from custom_components.carmabox.core.surplus_chain import (
            SurplusConfig,
            allocate_surplus,
        )

        # headroom = max_w - current_w = 30W; increase = min(5000, 30) = 30 ≤ 50
        consumer = _c(
            "heater", ctype="variable", min_w=100, max_w=130, current_w=100, is_running=True
        )
        cfg = SurplusConfig(min_surplus_w=20, start_delay_s=0)
        result = allocate_surplus(5000.0, [consumer], config=cfg)  # type: ignore[arg-type]
        heater_alloc = next(a for a in result.allocations if a.id == "heater")
        assert heater_alloc.action == "none"

    def test_reduce_break_when_deficit_met(self) -> None:
        """remaining ≤ 0 after first consumer → break loop (line 449)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            should_reduce_consumers,
        )

        # 2 consumers; first one covers the full deficit
        c1 = _c("big", priority=10, min_w=0, max_w=1000, current_w=1000, is_running=True)
        c2 = _c("small", priority=5, min_w=0, max_w=500, current_w=500, is_running=True)
        cfg = SurplusConfig(stop_delay_s=0)  # No delay
        hyst = HysteresisState()
        result = should_reduce_consumers(  # type: ignore[arg-type]
            1000.0, [c1, c2], hysteresis=hyst, config=cfg, now=time.monotonic()
        )
        # big (priority 10) stopped first; small should NOT appear (break hit)
        stopped_ids = [a.id for a in result if a.action == "stop"]
        assert "big" in stopped_ids
        assert "small" not in stopped_ids

    def test_reduce_skip_not_running(self) -> None:
        """Not running consumer → continue (line 451)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            should_reduce_consumers,
        )

        c_off = _c("idle", priority=10, is_running=False, max_w=1000)
        c_on = _c("active", priority=5, min_w=0, max_w=800, current_w=800, is_running=True)
        cfg = SurplusConfig(stop_delay_s=0)
        hyst = HysteresisState()
        result = should_reduce_consumers(  # type: ignore[arg-type]
            500.0, [c_off, c_on], hysteresis=hyst, config=cfg, now=time.monotonic()
        )
        stopped_ids = [a.id for a in result]
        # idle was not running → skipped
        assert "idle" not in stopped_ids
        assert "active" in stopped_ids

    def test_hysteresis_start_ok_first_time(self) -> None:
        """First call with surplus above threshold → start timer, return False (lines 504-505)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            _hysteresis_start_ok,
        )

        hyst = HysteresisState()
        cfg = SurplusConfig(start_delay_s=60.0)
        result = _hysteresis_start_ok("ev", surplus_w=2000, min_w=500, hyst=hyst, cfg=cfg, ts=100.0)
        assert result is False  # Timer started, not elapsed yet
        assert "ev" in hyst.surplus_above_since

    def test_hysteresis_stop_ok_first_time(self) -> None:
        """First call below threshold → start timer, return False (lines 522-524)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            _hysteresis_stop_ok,
        )

        hyst = HysteresisState()
        cfg = SurplusConfig(stop_delay_s=180.0)
        result = _hysteresis_stop_ok("ev", hyst=hyst, cfg=cfg, ts=100.0)
        assert result is False  # Timer started
        assert "ev" in hyst.surplus_below_since

    def test_hysteresis_stop_ok_delay_elapsed(self) -> None:
        """Delay elapsed → return True (line 525-526)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            _hysteresis_stop_ok,
        )

        hyst = HysteresisState()
        cfg = SurplusConfig(stop_delay_s=60.0)
        hyst.surplus_below_since["ev"] = 0.0  # Long ago
        result = _hysteresis_stop_ok("ev", hyst=hyst, cfg=cfg, ts=1000.0)
        assert result is True

    def test_update_allocation_appends_new(self) -> None:
        """consumer_id not in list → append new entry (lines 539-542)."""
        from custom_components.carmabox.core.surplus_chain import (
            SurplusAllocation,
            _update_allocation,
        )

        allocations: list[SurplusAllocation] = []
        _update_allocation(allocations, "pump", "start", 1500.0, "Starting pump")
        assert len(allocations) == 1
        assert allocations[0].id == "pump"
        assert allocations[0].action == "start"

    def test_is_export_allowed_skips_dependency_not_met(self) -> None:
        """dependency_met=False → continue (skip consumer) (line 633 continue)."""
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        # This ON_OFF consumer is not running — normally returns False.
        # But with dependency_met=False it's skipped → returns True.
        consumer = _c("pump", priority=1, ctype="on_off", is_running=False, dependency_met=False)
        result = is_export_allowed([consumer])  # type: ignore[arg-type]
        assert result is True

    def test_bump_hysteresis_blocks_continue(self) -> None:
        """Pass 3: hysteresis blocks bump for high-prio consumer → continue (line 360).

        Key: remaining must be LESS than high.min_w so we don't hit the
        'already fits' continue on line 345. surplus=500, min_w=2000 ensures
        remaining(500) < 2000 → enters bump path → hysteresis fires line 360.
        """
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            allocate_surplus,
        )

        # A (priority=5, needs 2000W) not started; B (priority=8) running 3000W
        # remaining=500 (< 2000), freeable=3000 → bump eligible
        # start_delay_s=60 + fresh hyst → _hysteresis_start_ok("bump_a") = False → line 360
        a = _c("a", priority=5, ctype="on_off", min_w=2000, max_w=2000, is_running=False)
        b = _c(
            "b", priority=8, ctype="on_off", min_w=0, max_w=3000, current_w=3000, is_running=True
        )
        cfg = SurplusConfig(start_delay_s=60.0, stop_delay_s=0, min_surplus_w=50)
        hyst = HysteresisState()  # Fresh — first time for bump timer
        result = allocate_surplus(  # type: ignore[arg-type]
            500.0, [a, b], hysteresis=hyst, config=cfg, now=100.0
        )
        # A should NOT be started (hysteresis blocked bump)
        assert not any(x.id == "a" and x.action == "start" for x in result.allocations)

    def test_bump_inner_loop_priority_skip_continue(self) -> None:
        """Pass 3 inner loop: low.priority ≤ high.priority → continue (line 366)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            allocate_surplus,
        )

        # A (priority=5, min_w=1000), E (priority=10, 2000W) can be freed,
        # B (priority=3) is running but priority ≤ 5 → inner loop hits line 366 continue
        a = _c("a", priority=5, ctype="on_off", min_w=1000, max_w=1000, is_running=False)
        b = _c("b", priority=3, ctype="on_off", min_w=0, max_w=500, current_w=500, is_running=True)
        e = _c(
            "e", priority=10, ctype="on_off", min_w=0, max_w=2000, current_w=2000, is_running=True
        )
        cfg = SurplusConfig(start_delay_s=0, stop_delay_s=0, min_surplus_w=50)
        hyst = HysteresisState()
        result = allocate_surplus(  # type: ignore[arg-type]
            200.0, [a, b, e], hysteresis=hyst, config=cfg, now=100.0
        )
        # A should be started (E provides enough freeable via bump)
        assert any(x.id == "a" and x.action == "start" for x in result.allocations)

    def test_bump_inner_loop_break_when_enough_freed(self) -> None:
        """Pass 3 inner loop: remaining+freed >= min_w → break (line 368).

        After D (priority=9) is freed (freed=200), at C's (priority=8) iteration:
        remaining(200)+freed(200)=400 >= A.min_w(300) → BREAK before freeing C.
        """
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            allocate_surplus,
        )

        # A (priority=5, min_w=300): remaining=200 < 300 → needs bump
        # D (priority=9, 200W) freed → then at C: remaining+freed=400 >= 300 → break!
        a = _c("a", priority=5, ctype="on_off", min_w=300, max_w=300, is_running=False)
        c = _c("c", priority=8, ctype="on_off", min_w=0, max_w=200, current_w=200, is_running=True)
        d = _c("d", priority=9, ctype="on_off", min_w=0, max_w=200, current_w=200, is_running=True)
        cfg = SurplusConfig(start_delay_s=0, stop_delay_s=0, min_surplus_w=50)
        hyst = HysteresisState()
        result = allocate_surplus(  # type: ignore[arg-type]
            200.0, [a, c, d], hysteresis=hyst, config=cfg, now=100.0
        )
        # A started; C should NOT be stopped (break fired before processing C)
        assert any(x.id == "a" and x.action == "start" for x in result.allocations)
        assert not any(x.id == "c" and x.action == "stop" for x in result.allocations)

    def test_is_export_allowed_variable_not_running_with_min(self) -> None:
        """VARIABLE not running with min_w > 0 → return False (line 633 branch)."""
        from custom_components.carmabox.core.surplus_chain import is_export_allowed

        # VARIABLE, not running, min_w=500 → export not allowed (could start it)
        consumer = _c(
            "battery",
            priority=2,
            ctype="variable",
            min_w=500,
            max_w=3000,
            is_running=False,
            dependency_met=True,
        )
        result = is_export_allowed([consumer])  # type: ignore[arg-type]
        assert result is False

    def test_hysteresis_start_ok_surplus_below_min_clears_timer(self) -> None:
        """surplus_w < min_w → pop timer, return False (lines 504-505)."""
        from custom_components.carmabox.core.surplus_chain import (
            HysteresisState,
            SurplusConfig,
            _hysteresis_start_ok,
        )

        hyst = HysteresisState()
        hyst.surplus_above_since["ev"] = 50.0  # Pre-existing timer
        cfg = SurplusConfig(start_delay_s=60.0)
        result = _hysteresis_start_ok("ev", surplus_w=100, min_w=500, hyst=hyst, cfg=cfg, ts=200.0)
        assert result is False  # Surplus dropped below min
        assert "ev" not in hyst.surplus_above_since  # Timer cleared

    def test_update_allocation_updates_existing_entry(self) -> None:
        """consumer_id already in list → update in-place (lines 539-542 update path)."""
        from custom_components.carmabox.core.surplus_chain import (
            SurplusAllocation,
            _update_allocation,
        )

        existing = SurplusAllocation("pump", "none", 0.0, 0.0, "")
        allocations = [existing]
        _update_allocation(allocations, "pump", "start", 1500.0, "Now starting")
        assert len(allocations) == 1  # No new entry added
        assert allocations[0].action == "start"
        assert allocations[0].target_w == 1500.0
