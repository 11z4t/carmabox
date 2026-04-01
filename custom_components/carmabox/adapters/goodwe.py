"""CARMA Box — GoodWe adapter.

Reads battery state and sends commands via HA's goodwe integration entities.

PLAT-1082: All Modbus writes serialized via asyncio.Lock to prevent
concurrent bus access (root cause of 2026-03-26 grid spike).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, ClassVar

from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from . import InverterAdapter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_RETRY_DELAY_S = 5
_MODBUS_MIN_INTERVAL_S = 0.1  # Min 100ms between Modbus calls
_ADAPTER_RATE_LIMIT_S = 2.0  # MANIFEST 10.2: max 1 call per 2s per adapter
_MAX_PEAK_SHAVING_W = 10000  # Safety clamp: inverter rated power
_VERIFY_DELAY_S = 1.0  # EXP-08: Wait before read-back verification


class GoodWeAdapter(InverterAdapter):
    """Adapter for GoodWe inverter via HA integration.

    Reads: SoC, battery power, temperature, EMS mode
    Writes: EMS mode, fast charging switch/power, peak shaving limit

    All writes are serialized through a shared Modbus lock to prevent
    concurrent bus access that caused the 2026-03-26 incident.
    """

    _modbus_lock: asyncio.Lock | None = None  # Shared across all instances

    @classmethod
    def _get_modbus_lock(cls) -> asyncio.Lock:
        """Get or create the shared Modbus lock (lazy init for event loop safety)."""
        if cls._modbus_lock is None:
            cls._modbus_lock = asyncio.Lock()
        return cls._modbus_lock

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        entity_prefix: str,
    ) -> None:
        """Initialize GoodWe adapter.

        Args:
            hass: Home Assistant instance.
            device_id: GoodWe device ID for goodwe.set_parameter calls.
            entity_prefix: Entity prefix (e.g. 'kontor', 'forrad').
        """
        self.hass = hass
        self.device_id = device_id
        self.prefix = entity_prefix
        self._last_call_time: float = 0.0

    def _state(self, entity_id: str, default: float = 0.0) -> float:
        """Read float state from HA entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _str_state(self, entity_id: str) -> str:
        """Read string state from HA entity."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return ""
        return state.state

    async def _safe_call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        """Call HA service with Modbus serialization, rate limiting, and retry.

        PLAT-1082: All writes go through shared asyncio.Lock to prevent
        concurrent Modbus bus access. Rate limited to max 1 call per 2s
        per adapter instance (MANIFEST 10.2).

        Returns True on success.
        """
        entity_id = data.get("entity_id", "?")

        if getattr(self, "_analyze_only", False):
            _LOGGER.info("DRY-RUN GoodWe %s: %s.%s → %s", self.prefix, domain, service, entity_id)
            return True

        lock = self._get_modbus_lock()
        async with lock:
            # Rate limit: enforce minimum interval between calls
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < _ADAPTER_RATE_LIMIT_S:
                await asyncio.sleep(_ADAPTER_RATE_LIMIT_S - elapsed)

            for attempt in range(2):
                try:
                    async with asyncio.timeout(30):  # 30s max per Modbus call
                        await self.hass.services.async_call(domain, service, data)
                    self._last_call_time = time.monotonic()
                    # Min delay before next Modbus call
                    await asyncio.sleep(_MODBUS_MIN_INTERVAL_S)
                    return True
                except ServiceNotFound:
                    _LOGGER.error(
                        "GoodWe %s: service not found %s.%s → %s",
                        self.prefix,
                        domain,
                        service,
                        entity_id,
                    )
                    return False
                except HomeAssistantError as err:
                    _LOGGER.error(
                        "GoodWe %s: HA error %s.%s → %s: %s (attempt %d/2)",
                        self.prefix,
                        domain,
                        service,
                        entity_id,
                        err,
                        attempt + 1,
                    )
                except Exception as err:
                    _LOGGER.exception(
                        "GoodWe %s: unexpected error %s.%s → %s: %s (attempt %d/2)",
                        self.prefix,
                        domain,
                        service,
                        entity_id,
                        err,
                        attempt + 1,
                    )
                if attempt == 0:
                    await asyncio.sleep(_RETRY_DELAY_S)
            self._last_call_time = time.monotonic()
            return False

    # ── Read ──────────────────────────────────────────────────

    @property
    def soc(self) -> float:
        """Battery SoC (0-100%). Returns -1 if unavailable."""
        return self._state(f"sensor.pv_battery_soc_{self.prefix}", default=-1.0)

    @property
    def power_w(self) -> float:
        """Battery power (W). Positive=discharge, negative=charge."""
        return self._state(f"sensor.goodwe_battery_power_{self.prefix}")

    @property
    def ems_mode(self) -> str:
        """Current EMS mode."""
        return self._str_state(f"select.goodwe_{self.prefix}_ems_mode")

    @property
    def fast_charging_on(self) -> bool:
        """Fast charging switch state."""
        return self._str_state(f"switch.goodwe_fast_charging_switch_{self.prefix}") == "on"

    @property
    def temperature_c(self) -> float | None:
        """Battery temperature (°C) or None if unavailable."""
        val = self._state(f"sensor.goodwe_battery_min_cell_temperature_{self.prefix}", -999)
        return val if val > -999 else None

    @property
    def bms_charge_limit_a(self) -> float:
        """BMS max charge current (A). Returns 0 if unavailable.

        BMS reduces this in cold weather (LFP lithium plating protection).
        At 5°C kontor battery showed only 1A — virtually no charging possible.
        """
        return self._state(f"sensor.goodwe_battery_charge_limit_{self.prefix}")

    @property
    def bms_discharge_limit_a(self) -> float:
        """BMS max discharge current (A). Returns 0 if unavailable.

        BMS reduces this in extreme cold. At 5°C kontor showed 22A (normal),
        but at 0°C can drop significantly.
        """
        return self._state(f"sensor.goodwe_battery_discharge_limit_{self.prefix}")

    @property
    def voltage(self) -> float:
        """Battery voltage (V). Typical ~400V for GoodWe Lynx LFP."""
        return self._state(f"sensor.goodwe_battery_voltage_{self.prefix}", default=400.0)

    @property
    def max_discharge_w(self) -> int:
        """Max discharge power based on BMS current limit and voltage.

        This is the ACTUAL hardware limit — requesting more will be ignored
        by the BMS. battery_balancer should use this to cap allocation.
        """
        limit_a = self.bms_discharge_limit_a
        if limit_a <= 0:
            return 0
        return int(limit_a * self.voltage)

    @property
    def max_charge_w(self) -> int:
        """Max charge power based on BMS current limit and voltage.

        Critical in cold weather — BMS may allow only 1A (400W) at 5°C.
        """
        limit_a = self.bms_charge_limit_a
        if limit_a <= 0:
            return 0
        return int(limit_a * self.voltage)

    @property
    def soh_pct(self) -> float:
        """State of Health (0-100%). Returns 100 if unavailable."""
        return self._state(f"sensor.goodwe_battery_soh_{self.prefix}", default=100.0)

    # ── Write ─────────────────────────────────────────────────

    # S7: Valid EMS modes — reject unknown values
    VALID_EMS_MODES = frozenset(
        {
            "charge_pv",
            "charge_battery",
            "discharge_pv",
            "discharge_battery",
            "battery_standby",
            "auto",
        }
    )

    # Map EMS modes to legacy desired_mode values
    _EMS_TO_DESIRED: ClassVar[dict[str, str]] = {
        "charge_pv": "charge",
        "charge_battery": "charge",
        "discharge_pv": "discharge",
        "discharge_battery": "discharge",
        "battery_standby": "wait",
        "auto": "wait",
    }

    async def set_ems_mode(self, mode: str, verify: bool = False) -> bool:
        """Set EMS mode with optional read-back verification.

        Also writes to legacy input_select.goodwe_{prefix}_desired_mode
        so that any legacy arbiter automations respect CARMA Box decisions.

        EXP-08: When verify=True, waits 1s and reads back the mode to confirm
        the Modbus write was accepted. Retries once on verify failure.
        """
        if mode not in self.VALID_EMS_MODES:
            _LOGGER.error("GoodWe %s: REJECTED invalid EMS mode '%s'", self.prefix, mode)
            return False
        _LOGGER.info("GoodWe %s: set EMS -> %s", self.prefix, mode)

        # Write to legacy desired_mode so arbiter doesn't override (best-effort)
        desired = self._EMS_TO_DESIRED.get(mode, "wait")
        with contextlib.suppress(Exception):
            await self.hass.services.async_call(
                "input_select",
                "select_option",
                {
                    "entity_id": f"input_select.goodwe_{self.prefix}_desired_mode",
                    "option": desired,
                },
            )

        ok = await self._safe_call(
            "select",
            "select_option",
            {
                "entity_id": f"select.goodwe_{self.prefix}_ems_mode",
                "option": mode,
            },
        )
        if not ok:
            return False
        # EXP-08: Verify write was accepted
        if verify:
            await asyncio.sleep(_VERIFY_DELAY_S)
            actual = self.ems_mode
            if actual != mode:
                _LOGGER.warning(
                    "GoodWe %s: EMS verify FAILED — wrote '%s', read '%s' — retrying",
                    self.prefix,
                    mode,
                    actual,
                )
                # Retry once
                ok = await self._safe_call(
                    "select",
                    "select_option",
                    {
                        "entity_id": f"select.goodwe_{self.prefix}_ems_mode",
                        "option": mode,
                    },
                )
                if ok:
                    await asyncio.sleep(_VERIFY_DELAY_S)
                    actual = self.ems_mode
                    if actual != mode:
                        _LOGGER.error(
                            "GoodWe %s: EMS verify FAILED after retry — '%s' != '%s'",
                            self.prefix,
                            actual,
                            mode,
                        )
                        return False

        # Reset ems_power_limit when switching to non-discharge modes
        # Prevents stale limits from previous discharge session
        if mode in ("charge_pv", "battery_standby"):
            with contextlib.suppress(Exception):
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {
                        "entity_id": f"number.goodwe_{self.prefix}_ems_power_limit",
                        "value": 0,
                    },
                )
        return True

    async def set_fast_charging(
        self,
        on: bool,
        power_pct: int = 100,
        soc_target: int = 100,
        authorized: bool = False,
    ) -> bool:
        """Set fast charging switch + power + SoC target.

        INV-3 HÅRDSPÄRR: fast_charging kan ALDRIG sättas ON utan
        explicit authorized=True. Detta förhindrar att gammal kod
        eller automationer aktiverar fast_charging oavsiktligt.
        """
        if on and not authorized:
            _LOGGER.warning(
                "GoodWe %s: BLOCKED fast_charging ON — authorized=False (INV-3)",
                self.prefix,
            )
            # Force OFF instead
            on = False

        switch_entity = f"switch.goodwe_fast_charging_switch_{self.prefix}"
        service = "turn_on" if on else "turn_off"
        ok = await self._safe_call(
            "switch",
            service,
            {"entity_id": switch_entity},
        )
        if not ok:
            return False
        if on:
            ok = await self._safe_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.goodwe_fast_charging_power_{self.prefix}",
                    "value": power_pct,
                },
            )
            if not ok:
                return False
            ok = await self._safe_call(
                "number",
                "set_value",
                {
                    "entity_id": f"number.goodwe_fast_charging_soc_{self.prefix}",
                    "value": soc_target,
                },
            )
            if not ok:
                return False
        return True

    async def set_discharge_limit(self, watts: int) -> bool:
        """Set EMS power limit (discharge rate in discharge_pv mode)."""
        _LOGGER.info("GoodWe %s: discharge limit -> %dW", self.prefix, watts)
        return await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.goodwe_{self.prefix}_ems_power_limit",
                "value": watts,
            },
        )

    async def set_peak_shaving_limit(self, watts: int) -> bool:
        """Set peak shaving power limit (register 47542).

        EXP-03: In peak_shaving operation mode, this controls the grid import
        target. Battery compensates everything ABOVE this limit.

        GoodWe support recommends peak_shaving mode + Modbus control for
        optimal reactive discharge. The firmware automatically adjusts
        discharge rate based on actual grid power vs this target.

        Args:
            watts: Max grid import allowed (W). Battery covers the rest.
                   0 = battery covers ALL grid import.
                   Clamped to 0-10000W for safety.
        """
        watts = max(0, min(watts, _MAX_PEAK_SHAVING_W))
        _LOGGER.info("GoodWe %s: peak_shaving_power_limit -> %dW", self.prefix, watts)
        return await self._safe_call(
            "number",
            "set_value",
            {
                "entity_id": f"number.goodwe_{self.prefix}_peak_shaving_power_limit",
                "value": watts,
            },
        )
