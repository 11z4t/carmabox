"""CARMA Box — Battery command implementations.

Standalone async command functions extracted from coordinator.py.
Each function is independently testable and takes explicit dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from homeassistant.core import HomeAssistant

    from ..adapters import InverterAdapter
    from ..optimizer.models import CarmaboxState
    from ..optimizer.safety_guard import SafetyGuard

from ..adapters.goodwe import GoodWeAdapter
from ..optimizer.models import BatteryCommand

_LOGGER = logging.getLogger(__name__)


async def cmd_charge_pv(
    hass: HomeAssistant,
    adapters: list[InverterAdapter],
    safety: SafetyGuard,
    state: CarmaboxState,
    last_command: BatteryCommand,
    temp_c: float | None,
    *,
    get_entity: Callable[[str], str | None] | None = None,
    read_float: Callable[[str | None], float] | None = None,
    safe_service_call: Callable[..., Coroutine[Any, Any, bool]] | None = None,
    check_write_verify: Callable[[str, str], None] | None = None,
    executor_enabled: bool = False,
) -> tuple[bool, int]:
    """Set batteries to charge from solar.

    SafetyGuard: heartbeat + rate limit + charge check.

    Args:
        hass: HomeAssistant instance (reserved for future use).
        adapters: List of inverter adapters (modern path). Empty list triggers legacy path.
        safety: SafetyGuard instance.
        state: Current CarmaboxState (used for battery_soc_1/2).
        last_command: Current BatteryCommand to detect duplicate sends.
        temp_c: Battery temperature in °C (or None if unavailable).
        get_entity: Legacy callback — resolves entity_id from config key.
        read_float: Legacy callback — reads float from HA entity state.
        safe_service_call: Legacy callback — calls HA service, returns success bool.
        check_write_verify: Legacy callback — queues deferred write verification.
        executor_enabled: Whether write-verify mode is active (legacy path).

    Returns:
        Tuple of (success, safety_blocks_delta).
        Coordinator must update _last_command / record_mode_change / save_runtime on success.
    """
    # IT-1939 BUG FIX: also skip re-send when already in taper mode
    if last_command in (
        BatteryCommand.CHARGE_PV,
        BatteryCommand.CHARGE_PV_TAPER,
    ):
        return False, 0

    # ── SafetyGuard gates (defense-in-depth) ─────────────
    heartbeat = safety.check_heartbeat()
    if not heartbeat.ok:
        _LOGGER.warning("SafetyGuard blocked charge_pv: %s", heartbeat.reason)
        return False, 1

    rate = safety.check_rate_limit()
    if not rate.ok:
        _LOGGER.info("SafetyGuard blocked charge_pv: %s", rate.reason)
        return False, 1

    charge_check = safety.check_charge(state.battery_soc_1, state.battery_soc_2, temp_c)
    if not charge_check.ok:
        _LOGGER.info("SafetyGuard blocked charge_pv: %s", charge_check.reason)
        return False, 1

    _LOGGER.info("CARMA: charge_pv (solar surplus)")
    success = False
    failed = False

    if adapters:
        for adapter in adapters:
            if adapter.soc >= 100:
                ok = await adapter.set_ems_mode("battery_standby")
            else:
                ok = await adapter.set_ems_mode("charge_pv")
                # INV-3: ALDRIG fast_charging i charge_pv — PV laddar utan det
                # fast_charging drar grid-import och bryter LAG 1
                if ok and isinstance(adapter, GoodWeAdapter):
                    await adapter.set_fast_charging(on=False)
            if ok:
                success = True
            else:
                failed = True

        # R3: Rollback on partial failure — force ALL to standby
        if failed and success:
            _LOGGER.warning("Partial charge_pv failure — rolling back all to standby")
            for adapter in adapters:
                await adapter.set_ems_mode("battery_standby")
            return False, 1
    else:
        # Legacy: raw entity-based control
        assert get_entity is not None
        assert read_float is not None
        assert safe_service_call is not None

        for ems_key in ("battery_ems_1", "battery_ems_2"):
            entity = get_entity(ems_key)
            if not entity:
                continue
            soc_key = ems_key.replace("ems", "soc")
            soc = read_float(get_entity(soc_key))
            mode = "battery_standby" if soc >= 100 else "charge_pv"
            if await safe_service_call(
                "select", "select_option", {"entity_id": entity, "option": mode}
            ):
                if executor_enabled and check_write_verify is not None:
                    check_write_verify(entity, mode)
                success = True
            else:
                failed = True

        # R3: Rollback on partial failure — force ALL to standby
        if failed and success:
            _LOGGER.warning("Partial charge_pv failure — rolling back all to standby (legacy)")
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = get_entity(ems_key)
                if entity:
                    await safe_service_call(
                        "select",
                        "select_option",
                        {"entity_id": entity, "option": "battery_standby"},
                    )
            return False, 1

    return success, 0
