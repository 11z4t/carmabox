"""EvBalancerCoordinator — DataUpdateCoordinator with cycle_s tick loop.

Implements spec §7 tick: READ → EXECUTE → REPORT.
Integrates: CooldownManager, EaseeClient, DecisionLog, translation_logic.
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    BRAIN_STALE_THRESHOLD_S,
    DEFAULT_CYCLE_S,
    DOMAIN,
    ENTITY_BRAIN_TARGET_EV_W,
    ENTITY_EASEE_ACTUAL_A,
    ENTITY_EASEE_CHARGER_ID,
    ENTITY_EASEE_DEVICE_ID,
    ENTITY_EASEE_PHASE_MODE,
    ENTITY_EASEE_POWER_W,
    ENTITY_EASEE_STATUS,
    ENTITY_EV_BALANCER_DISABLE,
    ENTITY_EV_CYCLE_S,
    ENTITY_EV_FAULT_COOLDOWN_S,
    ENTITY_EV_LINE_VOLTAGE,
    ENTITY_EV_MAX_PHYSICAL_FUSE_A,
    ENTITY_EV_MIN_DWELL_S,
    ENTITY_EV_MIN_START_SOC,
    ENTITY_EV_PAUSE_DWELL_S,
    ENTITY_EV_PHASE_COUNT,
    ENTITY_EV_PHASE_HEADROOM_A,
    ENTITY_EV_POST_PAUSE_COOLDOWN_S,
    ENTITY_EV_SOC_FILTERED,
    ENTITY_EV_SOFT_FUSE_RECOVERY_COOLDOWN_S,
    ENTITY_EV_TARGET_SOC,
    ENTITY_HOUSE_MAIN_FUSE_A,
    ENTITY_HOUSE_PHASE_A_1,
    ENTITY_HOUSE_PHASE_A_2,
    ENTITY_HOUSE_PHASE_A_3,
    CooldownType,
    EvAction,
    EvBalancerStatus,
)
from .cooldown_manager import CooldownManager
from .decision_log import DecisionLog
from .easee_client import EaseeClient
from .models import EvBalancerState, EvDecision, SensorSnapshot
from .translation_logic import translate_target_to_action

_LOGGER = logging.getLogger(__name__)

# Rolling window for 24h metrics
_24H_S = 86400


class EvBalancerCoordinator(DataUpdateCoordinator):
    """Coordinator that runs the EV balancer cycle every cycle_s seconds."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # we manage our own loop
        )
        self.hass = hass
        self.entry = entry
        self._state = EvBalancerState()
        self._cooldowns = CooldownManager()
        self._easee = EaseeClient(hass)
        self._decision_log: DecisionLog | None = None
        self._loop_task: asyncio.Task | None = None
        self._last_tick_ts: float = 0.0

        # Metric tracking (timestamps of events in last 24h)
        self._invariant_breach_ts: list[float] = []  # brain-offer clamp fires
        self._amp_change_ts: list[float] = []
        self._pause_resume_ts: list[float] = []
        self._fault_ts: list[float] = []
        self._soft_fuse_ts: list[float] = []

    async def _async_update_data(self) -> EvBalancerState:
        """Called by DataUpdateCoordinator on first refresh — initialise log."""
        if self._decision_log is None:
            self._decision_log = DecisionLog(self.hass.config.config_dir)
        return self._state

    async def async_config_entry_first_refresh(self) -> None:
        await super().async_config_entry_first_refresh()
        self._loop_task = self.hass.loop.create_task(self._tick_loop())

    def shutdown(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while True:
            try:
                cycle_s = self._read_float(ENTITY_EV_CYCLE_S, DEFAULT_CYCLE_S)
                await asyncio.sleep(cycle_s)
                now = time.monotonic()
                elapsed = now - self._last_tick_ts if self._last_tick_ts else cycle_s
                self._last_tick_ts = now
                await self._run_cycle(elapsed)
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.exception("ev_balancer tick error")
                await asyncio.sleep(DEFAULT_CYCLE_S)

    async def _run_cycle(self, elapsed_s: float) -> None:
        """One balancer cycle: READ → EXECUTE → REPORT."""
        self._cooldowns.tick(elapsed_s)
        self._purge_old_metrics()

        # Self-heal: clear charger_offline if Easee status is no longer error/offline.
        # Without this, a single failed API call locks the coordinator out permanently.
        if self._state.charger_offline:
            easee_status = self._read_str(ENTITY_EASEE_STATUS, "disconnected")
            if easee_status not in ("error", "offline"):
                _LOGGER.info("ev_balancer: charger_offline cleared (easee_status=%s)", easee_status)
                self._state.charger_offline = False

        snap = self._build_snapshot()
        decision = translate_target_to_action(snap, self._state, self._cooldowns)

        await self._execute(snap, decision)
        self._update_state(snap, decision)
        self._update_metrics(decision)

        if self._decision_log:
            await self._decision_log.async_log(snap, decision, self._state)

        # Sync safety guard corrections from EaseeClient into state
        self._state.safety_guard_corrections = self._easee.safety_guard_corrections

        self.async_set_updated_data(self._state)

    def _build_snapshot(self) -> SensorSnapshot:
        # Staleness guard: treat brain offer as 0 if entity not updated in 60s
        _brain_target_ev_w = 0.0
        _brain_state = self.hass.states.get(ENTITY_BRAIN_TARGET_EV_W)
        if _brain_state is not None and _brain_state.state not in ("unknown", "unavailable", ""):
            from homeassistant.util import dt as _dt

            _age_s = (_dt.utcnow() - _brain_state.last_changed).total_seconds()
            if _age_s <= BRAIN_STALE_THRESHOLD_S:
                try:
                    _brain_target_ev_w = float(_brain_state.state)
                except (ValueError, TypeError):
                    _brain_target_ev_w = 0.0
            else:
                _LOGGER.warning(
                    "ev_balancer: brain_target_ev_w stale %.0fs > %ds — clamping to 0",
                    _age_s,
                    BRAIN_STALE_THRESHOLD_S,
                )
                self._invariant_breach_ts.append(time.time())
        return SensorSnapshot(
            brain_target_ev_w=_brain_target_ev_w,
            ev_soc=self._read_float(ENTITY_EV_SOC_FILTERED, 0.0),
            ev_target_soc=self._read_float(ENTITY_EV_TARGET_SOC, 100.0),
            ev_min_start_soc=self._read_float(ENTITY_EV_MIN_START_SOC, 0.0),
            easee_status=self._read_str(ENTITY_EASEE_STATUS, "disconnected"),
            easee_phase_mode=self._read_str(ENTITY_EASEE_PHASE_MODE, "unknown"),
            easee_actual_a=self._read_float(ENTITY_EASEE_ACTUAL_A, 0.0),
            easee_power_w=self._read_float(ENTITY_EASEE_POWER_W, 0.0),
            house_phase_a_l1=self._read_float(ENTITY_HOUSE_PHASE_A_1, 0.0),
            house_phase_a_l2=self._read_float(ENTITY_HOUSE_PHASE_A_2, 0.0),
            house_phase_a_l3=self._read_float(ENTITY_HOUSE_PHASE_A_3, 0.0),
            phase_count=int(self._read_float(ENTITY_EV_PHASE_COUNT, 3)),
            line_voltage=self._read_float(ENTITY_EV_LINE_VOLTAGE, 230.0),
            house_main_fuse_a=self._read_float(ENTITY_HOUSE_MAIN_FUSE_A, 25.0),
            ev_phase_headroom_a=self._read_float(ENTITY_EV_PHASE_HEADROOM_A, 6.0),
            ev_max_physical_fuse_a=self._read_float(ENTITY_EV_MAX_PHYSICAL_FUSE_A, 16.0),
            min_a=6,
            max_a=16,
            cycle_s=self._read_float(ENTITY_EV_CYCLE_S, DEFAULT_CYCLE_S),
            min_dwell_s=self._read_float(ENTITY_EV_MIN_DWELL_S, 60.0),
            pause_dwell_s=self._read_float(ENTITY_EV_PAUSE_DWELL_S, 120.0),
            fault_cooldown_s=self._read_float(ENTITY_EV_FAULT_COOLDOWN_S, 60.0),
            soft_fuse_recovery_cooldown_s=self._read_float(
                ENTITY_EV_SOFT_FUSE_RECOVERY_COOLDOWN_S, 30.0
            ),
            post_pause_cooldown_s=self._read_float(ENTITY_EV_POST_PAUSE_COOLDOWN_S, 30.0),
            balancer_disabled=self._read_bool(ENTITY_EV_BALANCER_DISABLE, False),
        )

    async def _execute(self, snap: SensorSnapshot, decision: EvDecision) -> None:
        # Brain v3 single authority: always execute when Brain has a target,
        # even if balancer_disabled=on. Disabled flag suppresses own logic only.
        if snap.balancer_disabled and snap.brain_target_ev_w <= 0:
            return  # shadow mode: log only, no HW writes when Brain idle

        charger_id = self._read_str(ENTITY_EASEE_CHARGER_ID, "")
        device_id = self._read_str(ENTITY_EASEE_DEVICE_ID, "")

        if decision.action == EvAction.SET_DYNAMIC:
            ok = await self._easee.set_charger_dynamic_limit(device_id, decision.dynamic_a)
            if not ok:
                self._state.charger_offline = True

        elif decision.action == EvAction.STOP:
            await self._easee.set_charger_dynamic_limit(device_id, 0)
            self._state.last_dynamic_a = 0

        elif decision.action == EvAction.PAUSE:
            await self._easee.set_charger_dynamic_limit(device_id, 0)
            await self._easee.action_command(charger_id, "pause")
            self._cooldowns.start(CooldownType.POST_PAUSE, snap.post_pause_cooldown_s)

        elif decision.action == EvAction.RESUME:
            await self._easee.action_command(charger_id, "resume")
            # R3: immediately set dynamic to 6A
            await self._easee.set_charger_dynamic_limit(device_id, decision.dynamic_a)

    def _update_state(self, snap: SensorSnapshot, decision: EvDecision) -> None:
        self._state.status = decision.status
        self._state.status_reason = decision.reason.value if decision.reason else ""
        self._state.rejected_w = decision.rejected_w
        self._state.last_action = decision.action

        if decision.action == EvAction.SET_DYNAMIC:
            if decision.dynamic_a != self._state.last_dynamic_a:
                self._cooldowns.start(CooldownType.AMP_DWELL, snap.min_dwell_s)
            self._state.last_dynamic_a = decision.dynamic_a
            self._state.last_hw_write_ts = time.time()

        elif decision.action in (EvAction.STOP, EvAction.PAUSE):
            self._state.last_dynamic_a = 0
            self._state.last_hw_write_ts = time.time()
            self._cooldowns.start(CooldownType.PAUSE_DWELL, snap.pause_dwell_s)

        elif decision.action == EvAction.RESUME:
            self._state.last_dynamic_a = decision.dynamic_a
            self._state.last_hw_write_ts = time.time()

        if snap.charger_fault and decision.action == EvAction.STOP:
            self._cooldowns.start(CooldownType.FAULT_COOLDOWN, snap.fault_cooldown_s)

        if decision.status == EvBalancerStatus.SOFT_FUSE_THROTTLE:
            self._cooldowns.start(
                CooldownType.SOFT_FUSE_RECOVERY, snap.soft_fuse_recovery_cooldown_s
            )

        # Capability
        from .translation_logic import soft_fuse_cap_a

        cap = soft_fuse_cap_a(snap)
        self._state.capability_max_w = cap * snap.phase_count * snap.line_voltage
        self._state.capability_min_w = snap.min_charge_w

        # Dwell remaining (report to sensor)
        self._state.status_reason = decision.status_detail or self._state.status_reason

    def _update_metrics(self, decision: EvDecision) -> None:
        now = time.time()
        if decision.action == EvAction.SET_DYNAMIC:
            self._amp_change_ts.append(now)
        if decision.action in (EvAction.PAUSE, EvAction.RESUME):
            self._pause_resume_ts.append(now)
        if decision.status == EvBalancerStatus.FAULT:
            self._fault_ts.append(now)
        if decision.status == EvBalancerStatus.SOFT_FUSE_THROTTLE:
            self._soft_fuse_ts.append(now)

    def _purge_old_metrics(self) -> None:
        cutoff = time.time() - _24H_S
        self._invariant_breach_ts = [t for t in self._invariant_breach_ts if t > cutoff]
        self._amp_change_ts = [t for t in self._amp_change_ts if t > cutoff]
        self._pause_resume_ts = [t for t in self._pause_resume_ts if t > cutoff]
        self._fault_ts = [t for t in self._fault_ts if t > cutoff]
        self._soft_fuse_ts = [t for t in self._soft_fuse_ts if t > cutoff]

        self._state.amp_changes_24h = len(self._amp_change_ts)
        self._state.pause_resume_count_24h = len(self._pause_resume_ts)
        self._state.fault_count_24h = len(self._fault_ts)
        self._state.soft_fuse_engagements_24h = len(self._soft_fuse_ts)

    # ── HA state helpers ──────────────────────────────────────────────────────

    def _read_float(self, entity_id: str, default: float) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _read_str(self, entity_id: str, default: str) -> str:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        return state.state

    def _read_bool(self, entity_id: str, default: bool) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return default
        return state.state == "on"

    # ── Public properties (used by sensor.py) ─────────────────────────────────

    @property
    def invariant_breach_count_24h(self) -> int:
        """Number of brain-offer clamp or stale events in last 24h."""
        return len(self._invariant_breach_ts)

    @property
    def dwell_remaining_s(self) -> float:
        return max(
            self._cooldowns.remaining(CooldownType.AMP_DWELL),
            self._cooldowns.remaining(CooldownType.POST_PAUSE),
            self._cooldowns.remaining(CooldownType.PAUSE_DWELL),
        )

    @property
    def active_cooldowns(self) -> list[tuple[str, float]]:
        return self._cooldowns.active_cooldowns()
