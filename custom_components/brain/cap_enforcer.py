"""IT-4237 §3.1 — Balancer Cap Enforcement Layer (BCEL) v2.

HARD INVARIANT (Borje 2026-05-17 23:21): actual ≤ offer at all times.
No tolerance. Strict binary.

Called via hass.async_create_task(enforcer.tick()) from BrainController._async_tick()
every 5s. Tracks violation duration independently. When actual > offer for >
VIOLATION_GRACE_S seconds:
  - bat: write number.goodwe_{bank}_ems_power_limit = abs(distribution offer)
  - ev:  call easee.set_charger_dynamic_limit current = offer_a
  - Log CRITICAL + Slack [P1]

Cooldown: 30s between consecutive clamp actions (prevents Slack spam while
violation is being cleared by bat_balancer/Easee).

Async-pattern fix vs v1 (76a52374, reverted):
  - All service writes use blocking=False — fire-and-forget on HA event loop.
  - tick() is scheduled via hass.async_create_task() from Brain tick, NOT
    awaited directly. This prevents any risk of event-loop stall or
    'loop is not the running loop' if called from a different context.
  - State reads (hass.states.get) are synchronous and safe from any context.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

VIOLATION_GRACE_S: float = 5.0
CLAMP_COOLDOWN_S: float = 30.0
BAT_TOLERANCE_W: float = 100.0
EV_TOLERANCE_A: float = 2.0

ENTITY_DIST_K: str = "sensor.bat_balancer_distribution_kontor_w"
ENTITY_DIST_F: str = "sensor.bat_balancer_distribution_forrad_w"
ENTITY_ACT_K: str = "sensor.goodwe_battery_power_kontor"
ENTITY_ACT_F: str = "sensor.goodwe_battery_power_forrad"
ENTITY_EMS_K: str = "number.goodwe_kontor_ems_power_limit"
ENTITY_EMS_F: str = "number.goodwe_forrad_ems_power_limit"
ENTITY_EV_OFFER: str = "input_number.brain_target_ev_w"
ENTITY_EV_DYN_LIMIT: str = "sensor.easee_home_12840_dynamic_charger_limit"
EASEE_CHARGER_ID: str = "EH128405"
BCEL_PHASES: int = 3
BCEL_VOLTAGE_V: int = 230


class CapEnforcer:
    """Pre-validate every external write against Brain offer (IT-4237 §3.1-§3.5)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._bat_violation_since: float | None = None
        self._ev_violation_since: float | None = None
        self._last_bat_clamp_ts: float = 0.0
        self._last_ev_clamp_ts: float = 0.0

    def _read_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", "none", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def tick(self) -> None:
        """Check bat and EV invariants. Scheduled via async_create_task every Brain tick."""
        now = time.monotonic()
        await self._check_bat(now)
        await self._check_ev(now)

    async def _check_bat(self, now: float) -> None:
        dist_k = self._read_float(ENTITY_DIST_K)
        dist_f = self._read_float(ENTITY_DIST_F)
        act_k = self._read_float(ENTITY_ACT_K)
        act_f = self._read_float(ENTITY_ACT_F)

        if dist_k == 0.0 and dist_f == 0.0:
            self._bat_violation_since = None
            return

        offer_abs_k = abs(dist_k)
        offer_abs_f = abs(dist_f)
        actual_abs_k = abs(act_k)
        actual_abs_f = abs(act_f)

        violation = (
            actual_abs_k > offer_abs_k + BAT_TOLERANCE_W
            or actual_abs_f > offer_abs_f + BAT_TOLERANCE_W
        )

        if violation:
            if self._bat_violation_since is None:
                self._bat_violation_since = now
                _LOGGER.warning(
                    "BCEL bat violation start: K=%.0fW>%.0fW F=%.0fW>%.0fW",
                    actual_abs_k,
                    offer_abs_k,
                    actual_abs_f,
                    offer_abs_f,
                )
            elapsed = now - self._bat_violation_since
            cooldown_ok = (now - self._last_bat_clamp_ts) >= CLAMP_COOLDOWN_S
            if elapsed >= VIOLATION_GRACE_S and cooldown_ok:
                await self._clamp_bat(offer_abs_k, offer_abs_f, actual_abs_k, actual_abs_f, elapsed)
        else:
            if self._bat_violation_since is not None:
                _LOGGER.debug("BCEL bat violation cleared")
            self._bat_violation_since = None

    async def _clamp_bat(
        self,
        cap_k: float,
        cap_f: float,
        actual_k: float,
        actual_f: float,
        elapsed_s: float,
    ) -> None:
        self._last_bat_clamp_ts = time.monotonic()

        _LOGGER.critical(
            "BCEL BAT CLAMP after %.0fs: K %.0fW→%.0fW  F %.0fW→%.0fW",
            elapsed_s,
            actual_k,
            cap_k,
            actual_f,
            cap_f,
        )

        for entity, cap in [(ENTITY_EMS_K, cap_k), (ENTITY_EMS_F, cap_f)]:
            try:
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": entity, "value": round(cap)},
                    blocking=False,
                )
            except Exception as err:
                _LOGGER.error("BCEL bat clamp %s failed: %s", entity, err)

        await self._slack(
            f"🚨 *BCEL auto-clamp (bat)* ({{ts}}) — "
            f"actual K={actual_k:.0f}W F={actual_f:.0f}W > "
            f"offer K={cap_k:.0f}W F={cap_f:.0f}W ({elapsed_s:.0f}s). "
            f"ems_power_limit clamped. IT-4237",
            ":rotating_light:",
        )

    async def _check_ev(self, now: float) -> None:
        offer_w = self._read_float(ENTITY_EV_OFFER)
        if offer_w <= 0.0:
            self._ev_violation_since = None
            return

        offer_a = offer_w / BCEL_PHASES / BCEL_VOLTAGE_V
        actual_a = self._read_float(ENTITY_EV_DYN_LIMIT)

        if actual_a > offer_a + EV_TOLERANCE_A:
            if self._ev_violation_since is None:
                self._ev_violation_since = now
                _LOGGER.warning(
                    "BCEL EV violation start: actual=%.1fA > offer=%.1fA",
                    actual_a,
                    offer_a,
                )
            elapsed = now - self._ev_violation_since
            cooldown_ok = (now - self._last_ev_clamp_ts) >= CLAMP_COOLDOWN_S
            if elapsed >= VIOLATION_GRACE_S and cooldown_ok:
                await self._clamp_ev(offer_a, actual_a, elapsed)
        else:
            if self._ev_violation_since is not None:
                _LOGGER.debug("BCEL EV violation cleared")
            self._ev_violation_since = None

    async def _clamp_ev(self, cap_a: float, actual_a: float, elapsed_s: float) -> None:
        self._last_ev_clamp_ts = time.monotonic()
        clamp_a = max(0, round(cap_a))

        _LOGGER.critical(
            "BCEL EV CLAMP after %.0fs: actual=%.1fA → cap=%dA",
            elapsed_s,
            actual_a,
            clamp_a,
        )

        try:
            await self._hass.services.async_call(
                "easee",
                "set_charger_dynamic_limit",
                {"charger_id": EASEE_CHARGER_ID, "current": clamp_a},
                blocking=False,
            )
        except Exception as err:
            _LOGGER.error("BCEL EV clamp failed: %s", err)

        await self._slack(
            f"🚨 *BCEL auto-clamp (EV)* ({{ts}}) — "
            f"actual={actual_a:.1f}A > offer={cap_a:.1f}A ({elapsed_s:.0f}s). "
            f"dynamic_limit → {clamp_a}A. IT-4237",
            ":rotating_light:",
        )

    async def _slack(self, message: str, icon: str = ":rotating_light:") -> None:
        # DISABLED 2026-05-22 (Borje): BCEL Slack-spam pga GoodWe HW-autonomi.
        # Inverter respekterar inte ems_limit för charge vid PV-surplus →
        # CapEnforcer triggar var 20-50s. Slack-notify avstängd.
        return
