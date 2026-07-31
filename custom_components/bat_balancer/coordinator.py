"""BatBalancerCoordinator — 5s tick, distributes brain_target_bat_w to GoodWe banks."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BANK_SOC_CHARGE_CEILING_PCT,
    BANKS,
    BRAIN_OFFER_TOLERANCE_W,
    BRAIN_STALE_THRESHOLD_S,
    CURRENT_RESET_DEFAULT_A,
    CURRENT_RESET_SUSTAIN_S,
    CURRENT_TRIP_DEFAULT_A,
    ENTITY_BAT_ALLOW_HW_AUTONOMY,
    ENTITY_BAT_BALANCER_MODE,
    ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W,
    ENTITY_BAT_BATTERY_MODE,
    ENTITY_BAT_CHARGE_MAX_W,
    ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE,
    ENTITY_BAT_CURRENT_RESET_A,
    ENTITY_BAT_CURRENT_TRIP_A,
    ENTITY_BAT_CYCLE_SECONDS,
    ENTITY_BAT_DISCHARGE_MAX_W,
    ENTITY_BAT_ECO_MODE_DISABLE_SOC,
    ENTITY_BAT_HW_CLAMP_ACTIVATION_TICKS,
    ENTITY_BAT_HW_CLAMP_EMA_ALPHA,
    ENTITY_BAT_HW_CLAMP_RESET_TICKS,
    ENTITY_BAT_HW_CLAMP_TOLERANCE_W,
    ENTITY_BAT_INVERTER_RATED_W,
    ENTITY_BAT_SOC,
    ENTITY_BRAIN_OFFER_TOLERANCE_W,
    ENTITY_BRAIN_STALE_THRESHOLD_S,
    ENTITY_BRAIN_TARGET_BAT_W,
    ENTITY_CURRENT_RESET_SUSTAIN_S,
    ENTITY_GOODWE_BATTERY_POWER,
    ENTITY_GOODWE_DEVICE_ID,
    ENTITY_GOODWE_DOD_HOLDING,
    ENTITY_GOODWE_ECO_MODE_SOC,
    ENTITY_GOODWE_EMS_MODE,
    ENTITY_GOODWE_FAST_CHARGING,
    ENTITY_GOODWE_OPERATION_MODE,
    ENTITY_GOODWE_POWER_LIMIT,
    ENTITY_HOUSE_L1_CURRENT_A,
    ENTITY_HOUSE_L2_CURRENT_A,
    ENTITY_HOUSE_L3_CURRENT_A,
    ENTITY_HW_MISMATCH_THRESHOLD_W,
    ENTITY_HW_MISMATCH_TICKS,
    ENTITY_HW_STALE_THRESHOLD_S,
    ENTITY_MIN_SOC_BUFFER_PCT,
    ENTITY_SHADOW_MODE,
    ENTITY_SOC_CHARGE_CEILING_PCT,
    ENTITY_SOC_EQ_FULL_BIAS_HYSTERESIS_PCT,
    ENTITY_SOC_EQ_FULL_BIAS_THRESHOLD_PCT,
    ENTITY_SOC_EQ_MAX_ASYMMETRY_PCT,
    ENTITY_SOC_EQ_MAX_BIAS_W,
    ENTITY_SOC_EQ_THRESHOLD_PCT,
    GOODWE_ECO_MODE_ENABLE,
    GOODWE_ECO_MODE_SLOTS,
    GOODWE_EMS_MODE_CHARGE,
    GOODWE_EMS_MODE_DISCHARGE,
    GOODWE_EMS_MODE_STANDBY,
    GOODWE_MODE_BATTERY_STANDBY,
    GOODWE_MODE_PEAK_SHAVING,
    HW_CLAMP_ACTIVATION_TICKS_DEFAULT,
    HW_CLAMP_EMA_ALPHA_DEFAULT,
    HW_CLAMP_RESET_TICKS_DEFAULT,
    HW_CLAMP_TOLERANCE_DEFAULT_W,
    HW_MISMATCH_THRESHOLD_DEFAULT_W,
    HW_MISMATCH_TICKS_DEFAULT,
    HW_STALE_THRESHOLD_S,
    SOC_EQ_FULL_BIAS_HYSTERESIS_PCT,
    SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT,
    SOC_EQ_MAX_ASYMMETRY_DEFAULT_PCT,
    SOC_EQ_MAX_BIAS_DEFAULT_W,
    SOC_EQ_THRESHOLD_DEFAULT_PCT,
    BatBalancerStatus,
)
from .distribution_engine import distribute_target_to_banks
from .models import BankConfig, BankState, BatBalancerState, SensorSnapshot
from .sign_state_machine import SignStateMachine

_LOGGER = logging.getLogger(__name__)

_GOODWE_SOC_FALLBACK = "sensor.goodwe_battery_state_of_charge_{bank_id}"
_GOODWE_CHARGE_W_FALLBACK = "sensor.goodwe_battery_power_{bank_id}"


class BatBalancerCoordinator(DataUpdateCoordinator):
    """Reads brain target, distributes to banks via GoodWe EMS power limits."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="bat_balancer",
            update_interval=timedelta(seconds=5),
        )
        self._entry = entry
        self._bank_configs: dict[str, BankConfig] = {
            "kontor": BankConfig.default_kontor(),
            "forrad": BankConfig.default_forrad(),
        }
        self._sign_machines: dict[str, SignStateMachine] = {
            bid: SignStateMachine() for bid in BANKS
        }
        self._state = BatBalancerState()
        self._last_brain_write_ts: float = 0.0
        self._last_goodwe_ems_modes: dict[str, str | None] = dict.fromkeys(BANKS)
        self._last_goodwe_modes: dict[str, str | None] = dict.fromkeys(BANKS)
        self._bank_was_online: dict[str, bool] = dict.fromkeys(BANKS, True)
        self._hw_autonomy_initialized: bool = False
        # Q4: mismatch watchdog — tracks what ems_mode was actually written per bank
        self._last_written_ems_modes: dict[str, str | None] = dict.fromkeys(BANKS)
        self._hw_mismatch_ticks: dict[str, int] = dict.fromkeys(BANKS, 0)
        # Fix-B (IT-5674): per-bank off_grid-lock recovery timestamp (monotonic)
        self._off_grid_lock_recovery_ts: dict[str, float] = dict.fromkeys(BANKS, 0.0)
        # W1: current watchdog state
        self._current_emergency_active: bool = False
        self._current_below_reset_since: float | None = None
        # S2: hysteresis state for full-bias equalization
        self._full_bias_active: bool = False
        # S5: exact EMS limit written per bank (signed: neg=charge, pos=discharge)
        self._last_ems_limit_w: dict[str, float] = dict.fromkeys(BANKS, 0.0)
        # W6: HW closed-loop feedback-clamp state
        self._hw_actual_w: dict[str, float] = dict.fromkeys(BANKS, 0.0)
        self._hw_overshoot_ema: float = 0.0
        self._hw_overshoot_ticks: int = 0
        self._hw_undershoot_ticks: int = 0
        self._hw_correction_active: bool = False
        # REASON: current reason + per-bank reasons (updated each tick)
        self._reason: str = "initializing"
        self._bank_reasons: dict[str, str] = dict.fromkeys(BANKS, "initializing")

    # ── Public API (read by sensor.py) ──────────────────────────────────────

    @property
    def capability(self) -> dict:
        """Return dict exposed as sensor.bat_balancer_capability attributes."""
        bank_socs = []
        for bid in BANKS:
            bs = self._read_bank_state(bid)
            bank_socs.append(bs.current_soc)

        avg_soc = sum(bank_socs) / len(bank_socs) if bank_socs else 0.0
        min_soc = min(bank_socs) if bank_socs else 0.0

        # can_engage: any bank online + min_soc > buffer
        min_soc_buffer = self._float_helper(ENTITY_MIN_SOC_BUFFER_PCT, 20.0)
        any_online = any(self._read_bank_state(bid).is_online for bid in BANKS)
        can_engage = any_online and min_soc > min_soc_buffer

        # max_w_now: sum of discharge headroom across online banks
        max_discharge = sum(
            self._effective_discharge_w(bid)
            for bid in BANKS
            if self._read_bank_state(bid).is_online
        )
        max_charge = sum(
            self._effective_charge_w(bid) for bid in BANKS if self._read_bank_state(bid).is_online
        )

        status = str(self._state.status)
        if not any_online:
            status = str(BatBalancerStatus.OFFLINE_BANK)
            can_engage = False

        return {
            "can_engage": can_engage,
            "max_w_now": max_discharge if can_engage else 0.0,
            "max_charge_w_now": max_charge,
            "avg_soc": round(avg_soc, 1),
            "min_soc": round(min_soc, 1),
            "status": status,
            "sign_flip_pending": any(sm.sign_flip_pending for sm in self._sign_machines.values()),
            "last_distribution": dict(self._state.last_distribution),
        }

    @property
    def distribution_w(self) -> dict[str, float]:
        """Return last written EMS limit (signed: neg=charge, pos=discharge) per bank."""
        return dict(self._last_ems_limit_w)

    @property
    def avg_soc_pct(self) -> float:
        """Weighted average SoC across all banks (for sensor.bat_balancer_avg_soc_pct)."""
        total_kwh = sum(bc.capacity_kwh for bc in self._bank_configs.values())
        weighted = sum(
            self._read_bank_state(bid).current_soc * self._bank_configs[bid].capacity_kwh
            for bid in BANKS
        )
        return round(weighted / total_kwh if total_kwh > 0 else 0.0, 1)

    @property
    def hw_actual_w(self) -> dict[str, float]:
        """W6: actual HW power per bank (bat_balancer sign: pos=charge, neg=discharge)."""
        return dict(self._hw_actual_w)

    @property
    def hw_overshoot_w(self) -> float:
        """W6: current EMA-smoothed HW overshoot (W). Positive = HW delivers more than offer."""
        return round(self._hw_overshoot_ema, 1)

    @property
    def hw_correction_active(self) -> bool:
        """W6: True while adaptive EMA clamp is actively reducing effective_offer."""
        return self._hw_correction_active

    @property
    def reason(self) -> str:
        """REASON: why distributed_w may differ from offered_w (cross-balancer taxonomy)."""
        return self._reason

    @property
    def bank_reasons(self) -> dict[str, str]:
        """REASON: per-bank reason codes."""
        return dict(self._bank_reasons)

    # ── DataUpdateCoordinator hook ───────────────────────────────────────────

    async def _async_update_data(self):
        """Called by DataUpdateCoordinator — interval read from helper each tick."""
        cycle_s = max(5.0, min(60.0, self._float_helper(ENTITY_BAT_CYCLE_SECONDS, 5.0)))
        self.update_interval = timedelta(seconds=cycle_s)
        try:
            await self._tick()
            await self._check_hw_ems_mismatch()
        except Exception as exc:
            _LOGGER.error("bat_balancer tick error: %s", exc, exc_info=True)
            self._state.status = BatBalancerStatus.ERROR
            raise UpdateFailed(str(exc)) from exc
        return self._state

    async def _tick(self) -> None:
        await self._enforce_hw_autonomy_off()  # T2: watchdog runs every tick

        # W1: current watchdog — hard circuit breaker, highest priority
        if await self._check_current_safety():
            self._reason = "overcurrent"
            self._bank_reasons = dict.fromkeys(BANKS, "overcurrent")
            for bid in BANKS:
                # Force standby both banks every emergency tick (bypass both idempotent caches)
                self._last_goodwe_ems_modes[bid] = None
                self._last_goodwe_modes[bid] = None
                await self._write_power_limit(bid, 0.0)
            return

        shadow = self._bool_state(ENTITY_SHADOW_MODE, default=True)

        # B25: read effective target — template balancer_anti_export.yaml routes MANUAL/AUTO
        target_entity = self.hass.states.get(ENTITY_BAT_BALANCER_TARGET_EFFECTIVE_W)
        target_available = False
        target_w = 0.0
        if target_entity and target_entity.state not in ("unavailable", "unknown", None):
            try:
                target_w = float(target_entity.state)
                target_available = True
                self._last_brain_write_ts = time.monotonic()
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "bat_balancer: target entity state %r is not a valid float — treating as unavailable",
                    target_entity.state,
                )

        # stale target → zero
        brain_stale_s = int(
            self._float_helper(ENTITY_BRAIN_STALE_THRESHOLD_S, float(BRAIN_STALE_THRESHOLD_S))
        )
        if target_available:
            age = time.monotonic() - self._last_brain_write_ts
            if age > brain_stale_s:
                target_w = 0.0
                target_available = False

        # Design B (IT-2104-W4): pure-slave enforce — coordinator reads brain_offer directly and
        # clamps target_w to actual Brain offer. Prevents template bugs in balancer_anti_export.yaml
        # from causing autonomous discharge/charge.
        brain_offer_tol = self._float_helper(
            ENTITY_BRAIN_OFFER_TOLERANCE_W, BRAIN_OFFER_TOLERANCE_W
        )
        if target_available:
            brain_offer_w = self._float_helper(ENTITY_BRAIN_TARGET_BAT_W, 0.0)
            if abs(target_w - brain_offer_w) > brain_offer_tol:
                _LOGGER.warning(
                    "bat_balancer PURE-SLAVE CLAMP: target_effective=%.0fW brain_offer=%.0fW "
                    "(diff=%.0fW > tol=%.0fW) — clamping to brain_offer (IT-2104-W4)",
                    target_w,
                    brain_offer_w,
                    abs(target_w - brain_offer_w),
                    brain_offer_tol,
                )
                target_w = brain_offer_w
            # Hard cap (one-way): distribution must never exceed brain_offer, even within tolerance.
            # target_effective > brain_offer by any amount → cap to brain_offer.
            # target_effective < brain_offer → keep (underdelivery is always OK).
            elif abs(target_w) > abs(brain_offer_w):
                target_w = brain_offer_w

        # W6 OBSERVE: read HW actuals every tick (bat_balancer convention: pos=charge, neg=discharge)
        hw_kontor_raw = self._float_state(ENTITY_GOODWE_BATTERY_POWER.format(bank_id="kontor"), 0.0)
        hw_forrad_raw = self._float_state(ENTITY_GOODWE_BATTERY_POWER.format(bank_id="forrad"), 0.0)
        self._hw_actual_w["kontor"] = -hw_kontor_raw
        self._hw_actual_w["forrad"] = -hw_forrad_raw

        # W6 CORRECT: adaptive EMA clamp — enforce abs(HW) ≤ abs(offer) + HW_TOLERANCE_W
        if target_available and abs(target_w) > 1.0:
            hw_actual_sum = hw_kontor_raw + hw_forrad_raw
            hw_overshoot_w = abs(hw_actual_sum) - abs(target_w)
            alpha = self._float_helper(ENTITY_BAT_HW_CLAMP_EMA_ALPHA, HW_CLAMP_EMA_ALPHA_DEFAULT)
            self._hw_overshoot_ema = alpha * hw_overshoot_w + (1.0 - alpha) * self._hw_overshoot_ema
            tolerance_w = self._float_helper(
                ENTITY_BAT_HW_CLAMP_TOLERANCE_W, HW_CLAMP_TOLERANCE_DEFAULT_W
            )
            activation_ticks = int(
                self._float_helper(
                    ENTITY_BAT_HW_CLAMP_ACTIVATION_TICKS, HW_CLAMP_ACTIVATION_TICKS_DEFAULT
                )
            )
            reset_ticks = int(
                self._float_helper(ENTITY_BAT_HW_CLAMP_RESET_TICKS, HW_CLAMP_RESET_TICKS_DEFAULT)
            )
            if hw_overshoot_w > tolerance_w:
                self._hw_overshoot_ticks += 1
                self._hw_undershoot_ticks = 0
            else:
                self._hw_overshoot_ticks = 0
                if hw_overshoot_w < tolerance_w / 2.0:
                    self._hw_undershoot_ticks += 1
                else:
                    self._hw_undershoot_ticks = 0
            if self._hw_overshoot_ticks >= activation_ticks:
                self._hw_correction_active = True
            elif self._hw_undershoot_ticks >= reset_ticks:
                self._hw_correction_active = False
                self._hw_overshoot_ema = 0.0
                self._hw_undershoot_ticks = 0
            if self._hw_correction_active:
                correction_w = max(0.0, self._hw_overshoot_ema)
                effective_magnitude = max(0.0, abs(target_w) - correction_w)
                _LOGGER.info(
                    "bat_balancer W6-CLAMP active: hw=%.0fW offer=%.0fW ema=%.0fW → effective=%.0fW",
                    hw_actual_sum,
                    target_w,
                    self._hw_overshoot_ema,
                    effective_magnitude,
                )
                target_w = -effective_magnitude if target_w < 0.0 else effective_magnitude
        else:
            self._hw_correction_active = False
            self._hw_overshoot_ema = 0.0
            self._hw_overshoot_ticks = 0
            self._hw_undershoot_ticks = 0

        # --- read bank states ---
        bank_states: dict[str, BankState] = {bid: self._read_bank_state(bid) for bid in BANKS}

        # Warn when a bank transitions online→offline (or offline→online).
        for bid, bs in bank_states.items():
            was = self._bank_was_online.get(bid, True)
            if was and not bs.is_online:
                _LOGGER.warning(
                    "bat_balancer: bank %s went OFFLINE — all charge/discharge "
                    "redirected to remaining banks. Check GoodWe SoC sensor.",
                    bid,
                )
            elif not was and bs.is_online:
                _LOGGER.info("bat_balancer: bank %s back ONLINE.", bid)
            self._bank_was_online[bid] = bs.is_online

        # --- build snapshot ---
        # S2: hysteresis on full-bias equalization — prevents 518↔1134W oscillation
        # when SoC divergence jitters near the threshold.
        # Once in full-bias, exit only when divergence drops SOC_EQ_FULL_BIAS_HYSTERESIS_PCT below threshold.
        raw_full_bias_threshold = self._float_helper(
            ENTITY_SOC_EQ_FULL_BIAS_THRESHOLD_PCT, SOC_EQ_FULL_BIAS_THRESHOLD_DEFAULT_PCT
        )
        full_bias_hysteresis = self._float_helper(
            ENTITY_SOC_EQ_FULL_BIAS_HYSTERESIS_PCT, SOC_EQ_FULL_BIAS_HYSTERESIS_PCT
        )
        effective_full_bias_threshold = (
            max(0.0, raw_full_bias_threshold - full_bias_hysteresis)
            if self._full_bias_active
            else raw_full_bias_threshold
        )
        snapshot = SensorSnapshot(
            brain_target_bat_w=target_w,
            banks=bank_states,
            soc_equalization_threshold_pct=self._float_helper(
                ENTITY_SOC_EQ_THRESHOLD_PCT, SOC_EQ_THRESHOLD_DEFAULT_PCT
            ),
            soc_equalization_max_bias_w=self._float_helper(
                ENTITY_SOC_EQ_MAX_BIAS_W, SOC_EQ_MAX_BIAS_DEFAULT_W
            ),
            soc_equalization_full_bias_threshold_pct=effective_full_bias_threshold,
            soc_charge_ceiling_pct=self._float_helper(
                ENTITY_SOC_CHARGE_CEILING_PCT, BANK_SOC_CHARGE_CEILING_PCT
            ),
            soc_max_asymmetry_pct=self._float_helper(
                ENTITY_SOC_EQ_MAX_ASYMMETRY_PCT, SOC_EQ_MAX_ASYMMETRY_DEFAULT_PCT
            ),
            shadow_mode=shadow,
            brain_target_available=target_available,
        )

        if not target_available:
            self._state.status = BatBalancerStatus.OK
            self._state.last_target_w = 0.0
            self._reason = "shadow" if shadow else "ok"
            self._bank_reasons = dict.fromkeys(BANKS, self._reason)
            if not shadow:
                # write 0 to all banks (idle)
                for bid in BANKS:
                    ticked = self._sign_machines[bid].tick(0.0)
                    await self._write_power_limit(bid, ticked)
            return

        # --- distribute ---
        result = distribute_target_to_banks(
            target_w,
            self._bank_configs,
            bank_states,
            snapshot,
        )

        self._state.status = result.status
        self._state.last_target_w = target_w
        self._state.last_distribution = result.targets
        self._state.equalization_active = result.equalization_active

        # REASON: compute why distributed may differ from offer
        self._reason, self._bank_reasons = self._compute_reason(shadow, bank_states, result)
        # S2: track full-bias equalization state for hysteresis next tick
        # Full-bias is active when equalization_bias_max_w ≈ abs(target_w)
        self._full_bias_active = (
            result.equalization_active
            and abs(target_w) > 1.0
            and abs(result.equalization_bias_max_w - abs(target_w)) < 1.0
        )

        if shadow:
            self._state.status = BatBalancerStatus.SHADOW_MODE
            _LOGGER.debug(
                "bat_balancer: SHADOW — target=%.0fW dist=%s",
                target_w,
                {k: round(v) for k, v in result.targets.items()},
            )
            return

        # --- apply sign machine ---
        ticked_targets: dict[str, float] = {}
        for bid in BANKS:
            raw_target = result.targets.get(bid, 0.0)
            ticked_targets[bid] = self._sign_machines[bid].tick(raw_target)

        # Design D (IT-2104-W5): cross-direction guard — IT-2102 + Borje 2026-05-22 explicit.
        # Batteries MUST always operate in same direction. If any two non-standby banks have
        # opposing signs → force ALL STANDBY immediately. Catches sign-machine zero-tick
        # collisions and any residual template-sourced target flips.
        non_zero = {bid: t for bid, t in ticked_targets.items() if abs(t) > 0.5}
        directions = {(1 if t > 0 else -1) for t in non_zero.values()}
        if len(directions) > 1:
            _LOGGER.error(
                "bat_balancer CROSS-DIRECTION DETECTED: targets=%s — forcing ALL STANDBY (IT-2102)",
                {bid: round(t) for bid, t in ticked_targets.items()},
            )
            for bid in BANKS:
                self._last_goodwe_ems_modes[bid] = None
                self._last_goodwe_modes[bid] = None
                await self._write_power_limit(bid, 0.0)
            await self.hass.services.async_call(
                "notify",
                "mobile_app_bmq_iphone",
                {
                    "title": "bat_balancer CROSS-DIRECTION [P0]",
                    "message": (
                        "Cross-direction detected and stopped: "
                        + ", ".join(f"{b}={t:.0f}W" for b, t in ticked_targets.items())
                        + ". Båda banker satta till STANDBY."
                    ),
                },
                blocking=False,
            )
            return

        # --- write ---
        for bid in BANKS:
            await self._write_power_limit(bid, ticked_targets[bid], direction_w=target_w)

        _LOGGER.debug(
            "bat_balancer: tick target=%.0fW targets=%s status=%s",
            target_w,
            {k: round(v) for k, v in result.targets.items()},
            result.status,
        )

    async def _set_ems_mode(self, bank_id: str, mode: str) -> None:
        """Write GoodWe EMS mode — idempotent, fire-and-forget."""
        if self._last_goodwe_ems_modes.get(bank_id) == mode:
            return
        prev = self._last_goodwe_ems_modes.get(bank_id)
        self._last_goodwe_ems_modes[bank_id] = mode
        entity_id = ENTITY_GOODWE_EMS_MODE.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": mode},
                blocking=False,
            )
            _LOGGER.debug("bat_balancer: ems_mode %s -> %s", bank_id, mode)
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed to set ems_mode %s=%s: %s", entity_id, mode, exc)
            self._last_goodwe_ems_modes[bank_id] = prev

    async def _set_goodwe_mode(self, bank_id: str, mode: str) -> None:
        """Write GoodWe operation_mode — idempotent via _last_goodwe_modes cache (INV-19 revised).

        Only writes on mode transition. The EPS glitch (2026-05-02) was caused by repeated writes
        every tick; this cache ensures op_mode is written at most once per direction change.
        """
        if self._last_goodwe_modes.get(bank_id) == mode:
            return
        prev = self._last_goodwe_modes.get(bank_id)
        self._last_goodwe_modes[bank_id] = mode
        entity_id = ENTITY_GOODWE_OPERATION_MODE.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": mode},
                blocking=False,
            )
            _LOGGER.debug("bat_balancer: op_mode %s -> %s", bank_id, mode)
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed to set op_mode %s=%s: %s", entity_id, mode, exc)
            self._last_goodwe_modes[bank_id] = prev

    async def _write_power_limit(
        self, bank_id: str, target_w: float, *, direction_w: float | None = None
    ) -> None:
        """Write signed target_w to GoodWe: ems_mode + op_mode from global direction, magnitude per-bank.

        direction_w: global brain target (determines ems_mode for ALL banks uniformly).
                     Defaults to target_w when called standalone (tests / single-bank).
        Per-bank target_w may be 0W due to equalization bias — bank must still receive the
        correct global ems_mode (charge/discharge) with ems_limit=0, not battery_standby.

        Write order: ems_mode → op_mode → ems_power_limit.
        INV-19 (revised): op_mode written idempotently — once per transition, not every tick.
        The EPS glitch (2026-05-02) was caused by repeated writes; _last_goodwe_modes prevents that.
        """
        # IT-2102: global direction → ems_mode direction for all banks (Borje 2026-05-22 04:50).
        direction = direction_w if direction_w is not None else target_w
        if direction < 0:
            ems_mode = GOODWE_EMS_MODE_CHARGE
        elif direction > 0:
            ems_mode = GOODWE_EMS_MODE_DISCHARGE
        else:
            ems_mode = GOODWE_EMS_MODE_STANDBY

        op_mode = GOODWE_MODE_PEAK_SHAVING  # B23: required for GoodWe EMS to engage

        magnitude = abs(target_w)
        # W2 SAFETY: NEVER write discharge_battery|charge_battery + ems_limit=0.
        # GoodWe interprets discharge/charge+0W as uncapped → caused 24A + fuse trip 2026-05-22.
        # Per-bank target=0 (equalization bias) → battery_standby regardless of global direction.
        if round(magnitude, 0) == 0.0:
            if ems_mode != GOODWE_EMS_MODE_STANDBY:
                _LOGGER.warning(
                    "bat_balancer W2-SAFETY: clamped %s+0W → standby bank=%s "
                    "(global direction=%.0fW, IT-2102 overridden: SAFETY FIRST)",
                    ems_mode,
                    bank_id,
                    direction,
                )
            ems_mode = GOODWE_EMS_MODE_STANDBY
            op_mode = GOODWE_MODE_BATTERY_STANDBY  # Q1 OPT-A: prevents autonomous PV charge
            self._last_goodwe_ems_modes[bank_id] = (
                None  # force ems re-write every idle tick (HW residual fix)
            )
            # NOTE: _last_goodwe_modes NOT reset — op_mode written once on transition only (INV-19)

        self._last_written_ems_modes[bank_id] = ems_mode
        await self._set_ems_mode(bank_id, ems_mode)
        await self._set_goodwe_mode(bank_id, op_mode)

        # S5: track exact signed value written (neg=charge, pos=discharge)
        _sign = (
            -1.0
            if ems_mode == GOODWE_EMS_MODE_CHARGE
            else (1.0 if ems_mode == GOODWE_EMS_MODE_DISCHARGE else 0.0)
        )
        self._last_ems_limit_w[bank_id] = _sign * round(magnitude, 0)

        entity_id = ENTITY_GOODWE_POWER_LIMIT.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": round(magnitude, 0)},
                blocking=False,
            )
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed to write %s=%.0fW: %s", entity_id, magnitude, exc)

    # ── HW autonomy disable (T1 init + T2 watchdog) ─────────────────────────

    async def async_disable_hw_autonomy_on_init(self) -> None:
        """T1: On integration load, write eco_mode_soc=disable_val, dod_holding=off, fast_charging=off."""
        if self._bool_state(ENTITY_BAT_ALLOW_HW_AUTONOMY, False):
            _LOGGER.info("bat_balancer: allow_hw_autonomy=on — skipping init HW-autonomy disable")
            return
        eco_disable_soc = self._float_helper(ENTITY_BAT_ECO_MODE_DISABLE_SOC, 0.0)
        for bid in BANKS:
            await self._write_hw_autonomy_off(bid, eco_disable_soc, reason="init")
        self._hw_autonomy_initialized = True
        _LOGGER.info(
            "bat_balancer: HW autonomy disabled on init (banks=%s, eco_soc=%.0f)",
            BANKS,
            eco_disable_soc,
        )

    async def _enforce_hw_autonomy_off(self) -> None:
        """T2: Per-tick watchdog — re-enforce disable of GoodWe HW autonomy features."""
        if self._bool_state(ENTITY_BAT_ALLOW_HW_AUTONOMY, False):
            return
        eco_disable_soc = self._float_helper(ENTITY_BAT_ECO_MODE_DISABLE_SOC, 0.0)
        for bid in BANKS:
            eco_entity = ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id=bid)
            eco_val = self._float_state(eco_entity, eco_disable_soc)
            dod_entity = ENTITY_GOODWE_DOD_HOLDING.format(bank_id=bid)
            dod_on = self._bool_state(dod_entity, False)
            fc_entity = ENTITY_GOODWE_FAST_CHARGING.format(bank_id=bid)
            fc_on = self._bool_state(fc_entity, False)

            if eco_val == eco_disable_soc and not dod_on and not fc_on:
                continue

            violations: list[str] = []
            if eco_val != eco_disable_soc:
                violations.append(f"eco_mode_soc={eco_val:.0f}")
            if dod_on:
                violations.append("dod_holding=on")
            if fc_on:
                violations.append("fast_charging=on")

            _LOGGER.warning(
                "bat_balancer: HW autonomy override on bank %s: %s — re-enforcing",
                bid,
                ", ".join(violations),
            )
            await self._write_hw_autonomy_off(bid, eco_disable_soc, reason="watchdog")
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "bat_balancer: HW autonomy override [P0]",
                    "message": (
                        f"Bank {bid}: {', '.join(violations)} detected and reset to disabled. "
                        "Set input_boolean.bat_balancer_allow_hw_autonomy=on to allow HW autonomy."
                    ),
                    "notification_id": f"bat_balancer_hw_autonomy_{bid}",
                },
                blocking=False,
            )

    async def _write_hw_autonomy_off(
        self, bank_id: str, eco_disable_soc: float, *, reason: str
    ) -> None:
        """Write eco_mode_soc=disable_val, dod_holding=off, fast_charging=off for one bank.

        Also disables eco_mode_enable + all 4 eco_mode slot-switches via goodwe.set_parameter
        (these are not exposed as number/switch entities — only reachable via the goodwe service).
        """
        eco_entity = ENTITY_GOODWE_ECO_MODE_SOC.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": eco_entity, "value": eco_disable_soc},
                blocking=False,
            )
            _LOGGER.debug(
                "bat_balancer: [%s] eco_mode_soc_%s=%.0f", reason, bank_id, eco_disable_soc
            )
        except Exception as exc:
            _LOGGER.warning(
                "bat_balancer: failed set %s=%.0f: %s", eco_entity, eco_disable_soc, exc
            )

        # Disable eco_mode_enable (master switch) + all 4 slot-switches via goodwe.set_parameter
        device_id = self._str_state(ENTITY_GOODWE_DEVICE_ID.format(bank_id=bank_id), "")
        if device_id:
            for param in (GOODWE_ECO_MODE_ENABLE, *GOODWE_ECO_MODE_SLOTS):
                try:
                    await self.hass.services.async_call(
                        "goodwe",
                        "set_parameter",
                        {"device_id": device_id, "parameter": param, "value": 0},
                        blocking=False,
                    )
                    _LOGGER.debug("bat_balancer: [%s] goodwe %s %s=0", reason, bank_id, param)
                except Exception as exc:
                    _LOGGER.warning(
                        "bat_balancer: failed goodwe.set_parameter %s %s=0: %s",
                        bank_id,
                        param,
                        exc,
                    )
        else:
            _LOGGER.warning(
                "bat_balancer: no device_id for bank %s — eco_mode slots NOT disabled. "
                "Set %s to the GoodWe HA device registry ID.",
                bank_id,
                ENTITY_GOODWE_DEVICE_ID.format(bank_id=bank_id),
            )

        dod_entity = ENTITY_GOODWE_DOD_HOLDING.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": dod_entity},
                blocking=False,
            )
            _LOGGER.debug("bat_balancer: [%s] dod_holding_%s=off", reason, bank_id)
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed turn_off %s: %s", dod_entity, exc)

        fc_entity = ENTITY_GOODWE_FAST_CHARGING.format(bank_id=bank_id)
        try:
            await self.hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": fc_entity},
                blocking=False,
            )
            _LOGGER.debug("bat_balancer: [%s] fast_charging_%s=off", reason, bank_id)
        except Exception as exc:
            _LOGGER.warning("bat_balancer: failed turn_off %s: %s", fc_entity, exc)

    # ── HW-EMS mismatch watchdog (Q4) ───────────────────────────────────────

    async def _check_hw_ems_mismatch(self) -> None:
        """Q4: Detect when GoodWe HW ignores coordinator EMS writes.

        Compares last written ems_mode against actual sensor.goodwe_battery_power_{bank_id}.
        Fires persistent_notification + WARNING after HW_MISMATCH_TICKS_DEFAULT consecutive ticks.
        Clears notification when mismatch resolves.
        """
        threshold_w = self._float_helper(
            ENTITY_HW_MISMATCH_THRESHOLD_W, HW_MISMATCH_THRESHOLD_DEFAULT_W
        )
        mismatch_ticks = int(
            self._float_helper(ENTITY_HW_MISMATCH_TICKS, float(HW_MISMATCH_TICKS_DEFAULT))
        )
        for bid in BANKS:
            last_ems = self._last_written_ems_modes.get(bid)
            if last_ems is None:
                self._hw_mismatch_ticks[bid] = 0
                continue

            hw_power = self._float_state(ENTITY_GOODWE_BATTERY_POWER.format(bank_id=bid), 0.0)

            mismatch = (
                (last_ems == GOODWE_EMS_MODE_STANDBY and abs(hw_power) > threshold_w)
                or (last_ems == GOODWE_EMS_MODE_DISCHARGE and hw_power < -threshold_w)
                or (last_ems == GOODWE_EMS_MODE_CHARGE and hw_power > threshold_w)
            )

            if mismatch:
                self._hw_mismatch_ticks[bid] += 1
                if self._hw_mismatch_ticks[bid] == mismatch_ticks:
                    _LOGGER.warning(
                        "bat_balancer: HW-EMS MISMATCH bank=%s wrote=%s hw_power=%.0fW "
                        "(threshold=±%.0fW) — GoodWe not following EMS command",
                        bid,
                        last_ems,
                        hw_power,
                        threshold_w,
                    )
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "bat_balancer: HW-EMS mismatch [P1]",
                            "message": (
                                f"Bank {bid}: wrote ems_mode={last_ems} but "
                                f"hw_power={hw_power:.0f}W (threshold=±{threshold_w:.0f}W). "
                                "GoodWe is not following EMS — likely autonomous PV charge or "
                                "inverter offline. Check inverter status and HA logs."
                            ),
                            "notification_id": f"bat_balancer_hw_mismatch_{bid}",
                        },
                        blocking=False,
                    )
                    # Fix-B (IT-5674): per-bank off_grid-lock recovery.
                    # Cross-direction mismatch = GoodWe locked ignoring EMS direction.
                    # Force EMS + op_mode cache-bust so next tick re-sends the commands.
                    is_cross_dir = (
                        last_ems == GOODWE_EMS_MODE_DISCHARGE and hw_power < -threshold_w
                    ) or (last_ems == GOODWE_EMS_MODE_CHARGE and hw_power > threshold_w)
                    import time as _time_fix_b

                    now_mono = _time_fix_b.monotonic()
                    if (
                        is_cross_dir
                        and (now_mono - self._off_grid_lock_recovery_ts.get(bid, 0.0)) > 60.0
                    ):
                        _LOGGER.warning(
                            "bat_balancer Fix-B: off_grid-lock bank=%s wrote=%s hw=%.0fW "
                            "— cache-busting EMS+op_mode to break lock",
                            bid,
                            last_ems,
                            hw_power,
                        )
                        self._last_goodwe_ems_modes[bid] = None
                        self._last_goodwe_modes[bid] = None
                        self._hw_mismatch_ticks[bid] = 0
                        self._off_grid_lock_recovery_ts[bid] = now_mono
            else:
                prev_ticks = self._hw_mismatch_ticks.get(bid, 0)
                self._hw_mismatch_ticks[bid] = 0
                if prev_ticks >= mismatch_ticks:
                    _LOGGER.info("bat_balancer: HW-EMS mismatch cleared bank=%s", bid)
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "dismiss",
                        {"notification_id": f"bat_balancer_hw_mismatch_{bid}"},
                        blocking=False,
                    )

    # ── W1: Current watchdog (P0 safety) ────────────────────────────────────

    async def _check_current_safety(self) -> bool:
        """W1: Hard circuit breaker — grid current > trip_a → emergency stop.

        Returns True while emergency is active (caller must skip normal writes).
        Clears automatically when max(L1,L2,L3) < reset_a sustained CURRENT_RESET_SUSTAIN_S.
        """
        trip_a = self._float_helper(ENTITY_BAT_CURRENT_TRIP_A, CURRENT_TRIP_DEFAULT_A)
        reset_a = self._float_helper(ENTITY_BAT_CURRENT_RESET_A, CURRENT_RESET_DEFAULT_A)
        reset_sustain_s = int(
            self._float_helper(ENTITY_CURRENT_RESET_SUSTAIN_S, float(CURRENT_RESET_SUSTAIN_S))
        )

        l1 = abs(self._float_state(ENTITY_HOUSE_L1_CURRENT_A, 0.0))
        l2 = abs(self._float_state(ENTITY_HOUSE_L2_CURRENT_A, 0.0))
        l3 = abs(self._float_state(ENTITY_HOUSE_L3_CURRENT_A, 0.0))
        peak_a = max(l1, l2, l3)

        if peak_a > trip_a:
            self._current_below_reset_since = None
            if not self._current_emergency_active:
                self._current_emergency_active = True
                _LOGGER.error(
                    "bat_balancer W1 EMERGENCY: peak_current=%.1fA > trip=%.1fA "
                    "(L1=%.1f L2=%.1f L3=%.1f) — EMERGENCY STOP both banks",
                    peak_a,
                    trip_a,
                    l1,
                    l2,
                    l3,
                )
                await self.hass.services.async_call(
                    "input_boolean",
                    "turn_on",
                    {"entity_id": ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE},
                    blocking=False,
                )
                await self.hass.services.async_call(
                    "notify",
                    "mobile_app_bmq_iphone",
                    {
                        "title": "bat_balancer W1 STRÖMSKYDD [P0]",
                        "message": (
                            f"Emergency stop: {peak_a:.1f}A > {trip_a:.1f}A "
                            f"(L1={l1:.1f} L2={l2:.1f} L3={l3:.1f}A). "
                            "Batteristyrning stoppad."
                        ),
                    },
                    blocking=False,
                )
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "bat_balancer W1 STRÖMSKYDD [P0]",
                        "message": (
                            f"Emergency stop aktiverat: peak={peak_a:.1f}A > trip={trip_a:.1f}A. "
                            f"L1={l1:.1f} L2={l2:.1f} L3={l3:.1f}A. "
                            f"Återgår till normal drift när ström < {reset_a:.1f}A i {reset_sustain_s}s."
                        ),
                        "notification_id": "bat_balancer_current_emergency",
                    },
                    blocking=False,
                )
            return True

        if self._current_emergency_active:
            if peak_a < reset_a:
                if self._current_below_reset_since is None:
                    self._current_below_reset_since = time.monotonic()
                elapsed = time.monotonic() - self._current_below_reset_since
                if elapsed >= reset_sustain_s:
                    self._current_emergency_active = False
                    self._current_below_reset_since = None
                    _LOGGER.info(
                        "bat_balancer W1: emergency CLEARED — peak=%.1fA < reset=%.1fA sustained %ds",
                        peak_a,
                        reset_a,
                        reset_sustain_s,
                    )
                    await self.hass.services.async_call(
                        "input_boolean",
                        "turn_off",
                        {"entity_id": ENTITY_BAT_CURRENT_EMERGENCY_ACTIVE},
                        blocking=False,
                    )
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "dismiss",
                        {"notification_id": "bat_balancer_current_emergency"},
                        blocking=False,
                    )
            else:
                self._current_below_reset_since = None
            return self._current_emergency_active  # False once cleared above, True while waiting

        return False

    def shutdown(self) -> None:
        """Called on unload — reset sign machines."""
        for sm in self._sign_machines.values():
            sm.reset()

    def _compute_reason(
        self,
        shadow: bool,
        bank_states: dict,
        result,
    ) -> tuple[str, dict[str, str]]:
        """Compute cross-balancer reason code for this tick (priority order)."""
        if self._current_emergency_active:
            r = "overcurrent"
            return r, dict.fromkeys(BANKS, r)

        if shadow:
            return "shadow", dict.fromkeys(BANKS, "shadow")

        mode_state = self.hass.states.get(ENTITY_BAT_BALANCER_MODE)
        if mode_state and mode_state.state == "MANUAL":
            return "manual", dict.fromkeys(BANKS, "manual")

        if not any(bs.is_online for bs in bank_states.values()):
            return "hw_not_available", dict.fromkeys(BANKS, "hw_not_available")

        online_bank_ids = {bid for bid, bs in bank_states.items() if bs and bs.is_online}
        _ceil_ids = result.soc_ceil_bank_ids
        soc_ceil_banks = (
            (_ceil_ids & online_bank_ids) if isinstance(_ceil_ids, frozenset) else frozenset()
        )

        # W6: HW delivers more than offer despite our writes
        if self._hw_correction_active:
            top = "offer_above_max"
        elif soc_ceil_banks:
            top = "soc_ceil"
        elif (
            result.bms_cap_suppressed_w > 0.0
            or result.rejected_reason == "bms_cap_aggregate"
            or result.overflow_redistributed
        ):
            top = "bms_cap"
        else:
            top = "ok"

        # Per-bank reasons
        bank_reasons: dict[str, str] = {}
        for bid in BANKS:
            bs = bank_states.get(bid)
            if bs is None or not bs.is_online:
                bank_reasons[bid] = "hw_not_available"
            elif bid in soc_ceil_banks:
                bank_reasons[bid] = "soc_ceil"
            elif bid in result.capped_bank_ids:
                bank_reasons[bid] = "bms_cap"
            else:
                bank_reasons[bid] = "ok"

        return top, bank_reasons

    # ── State readers ────────────────────────────────────────────────────────

    def _read_bank_state(self, bank_id: str) -> BankState:
        """Read live sensor data for one bank."""
        # SoC: try filtered first, fall back to GoodWe raw
        soc_entity = ENTITY_BAT_SOC.format(bank_id=bank_id)
        soc_state = self.hass.states.get(soc_entity)
        sensor_stale = False

        if soc_state is None or soc_state.state in ("unavailable", "unknown"):
            # fallback
            fallback_id = _GOODWE_SOC_FALLBACK.format(bank_id=bank_id)
            soc_state = self.hass.states.get(fallback_id)

        soc = 50.0  # safe default
        is_online = False
        if soc_state and soc_state.state not in ("unavailable", "unknown", None):
            try:
                soc = float(soc_state.state)
                is_online = True
                # stale check — use dt_util (wallclock) not hass.loop.time() (monotonic)
                age_s = (dt_util.utcnow() - soc_state.last_updated).total_seconds()
                hw_stale_s = int(
                    self._float_helper(ENTITY_HW_STALE_THRESHOLD_S, float(HW_STALE_THRESHOLD_S))
                )
                if age_s > hw_stale_s:
                    sensor_stale = True
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "bat_balancer: HW staleness check failed for bank %s — treating as stale",
                    bank_id,
                )

        # BMS dynamic caps — bypassed in MANUAL mode (A1: prevents BMS-dyn swing)
        in_manual = self._str_state(ENTITY_BAT_BALANCER_MODE, "AUTO") == "MANUAL"
        if in_manual:
            rated_w = self._float_helper(
                ENTITY_BAT_INVERTER_RATED_W.format(bank_id=bank_id), 5000.0
            )
            bms_charge: float | None = rated_w
            bms_discharge: float | None = rated_w
        else:
            bms_charge = self._optional_float(ENTITY_BAT_CHARGE_MAX_W.format(bank_id=bank_id))
            bms_discharge = self._optional_float(ENTITY_BAT_DISCHARGE_MAX_W.format(bank_id=bank_id))

        battery_mode = self._str_state(
            ENTITY_BAT_BATTERY_MODE.format(bank_id=bank_id), "battery_standby"
        )

        return BankState(
            bank_id=bank_id,
            current_soc=soc,
            is_online=is_online,
            battery_mode=battery_mode,
            bms_max_charge_w=bms_charge,
            bms_max_discharge_w=bms_discharge,
            sensor_stale=sensor_stale,
        )

    def _effective_discharge_w(self, bank_id: str) -> float:
        bs = self._read_bank_state(bank_id)
        bc = self._bank_configs[bank_id]
        if bs.bms_max_discharge_w is not None and not bs.sensor_stale:
            return bs.bms_max_discharge_w
        return bc.max_discharge_w

    def _effective_charge_w(self, bank_id: str) -> float:
        bs = self._read_bank_state(bank_id)
        bc = self._bank_configs[bank_id]
        if bs.bms_max_charge_w is not None and not bs.sensor_stale:
            return bs.bms_max_charge_w
        return bc.max_charge_w

    # ── HA state helpers ────────────────────────────────────────────────────

    def _float_state(self, entity_id: str, default: float) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return default
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return default

    def _float_helper(self, entity_id: str, default: float) -> float:
        return self._float_state(entity_id, default)

    def _bool_state(self, entity_id: str, default: bool) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return default
        return state.state == "on"

    def _str_state(self, entity_id: str, default: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return default
        return state.state

    def _optional_float(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None
