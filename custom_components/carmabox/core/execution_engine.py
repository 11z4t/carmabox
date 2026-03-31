"""ExecutionEngine — hårdvarukontroll och plankörning (PLAT-1141 COORD-02).

Extraherad från coordinator.py. Innehåller:
- execute_v2: plan-driven execution med battery balancer + surplus chain
- execute_surplus_allocations: utför surplus chain-allokeringar mot hårdvara
- enforce_ems_modes: säkerhetsenforcement av EMS-lägen varje cykel
- cmd_miner, cmd_ev_start, cmd_ev_stop, cmd_ev_adjust: EV/miner-kommandon
- cmd_charge_pv, cmd_grid_charge, cmd_standby, cmd_discharge: batterikommandon
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..optimizer.models import CarmaboxState
    from .surplus_chain import SurplusAllocation

from ..adapters.goodwe import GoodWeAdapter
from ..const import (
    DEFAULT_BATTERY_1_KWH,
    DEFAULT_BATTERY_2_KWH,
    DEFAULT_EV_MAX_AMPS,
    DEFAULT_EV_MIN_AMPS,
    DEFAULT_EV_NIGHT_TARGET_SOC,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_NIGHT_WEIGHT,
    EV_RAMP_STEPS,
    PLAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class ExecutionEngine:
    """Kör batteri-, EV- och surplus-kommandon på uppdrag av koordinatorn.

    Tar koordinatorn som dependency injection — accederar koordinatorns
    attribut via self._coord.X. Metoder kan anropa varandra direkt
    (t.ex. execute_v2 anropar cmd_ev_start) utan omväg via koordinatorn.
    """

    def __init__(self, coordinator: Any) -> None:
        """Initialisera ExecutionEngine med koordinatorinstansen.

        Args:
            coordinator: CarmaboxCoordinator-instansen som äger
                         hårdvaruadaptrarna och tillståndsattributen.
        """
        self._coord = coordinator

    # ═══════════════════════════════════════════════════════════════════════
    # execute_v2 — plan-driven execution (PLAT-1141)
    # ═══════════════════════════════════════════════════════════════════════

    async def execute_v2(self, state: CarmaboxState) -> None:
        """V2-exekvering — plandriven, använder core-moduler.

        Flöde: Plan Executor → Battery Balancer → Surplus Chain.
        Grid Guard har redan kört (Layer 0).

        Args:
            state: Aktuellt systemtillstånd (sensorer).
        """
        from .battery_balancer import (
            BatteryInfo,
            calculate_proportional_discharge,
        )
        from .plan_executor import (
            ExecutorConfig,
            ExecutorState,
            PlanAction,
            check_replan_needed,
            execute_plan_hour,
        )
        from .surplus_chain import (
            SurplusConfig,
            allocate_surplus,
            should_reduce_consumers,
        )

        now = datetime.now()
        hour = now.hour
        opts = self._coord._cfg

        _LOGGER.warning("V2: h=%d soc=%.0f", hour, state.total_battery_soc)

        # ── Find plan action for current hour ───────────────────
        planned = next((p for p in self._coord.plan if p.hour == hour), None)
        plan_action = None
        if planned:
            plan_action = PlanAction(
                hour=planned.hour,
                action=planned.action,
                battery_kw=planned.battery_kw,
                grid_kw=planned.grid_kw,
                price=planned.price,
                battery_soc=planned.battery_soc,
                ev_soc=planned.ev_soc,
            )

        # ── Build executor state ────────────────────────────────
        is_night = hour >= DEFAULT_NIGHT_START or hour < DEFAULT_NIGHT_END
        weight = DEFAULT_NIGHT_WEIGHT if is_night else 1.0
        headroom = self._coord._grid_guard.headroom_kw if self._coord._grid_guard_result else 1.0
        ev_connected = (
            (self._coord.ev_adapter and self._coord.ev_adapter.cable_locked)
            if self._coord.ev_adapter
            else False
        )

        exec_state = ExecutorState(
            grid_import_w=state.grid_power_w,
            pv_power_w=state.pv_power_w,
            battery_soc_1=state.battery_soc_1,
            battery_soc_2=state.battery_soc_2,
            battery_power_1=state.battery_power_1,
            battery_power_2=state.battery_power_2,
            ev_power_w=state.ev_power_w,
            ev_soc=state.ev_soc,
            ev_connected=ev_connected,
            current_price=self._coord._read_float(opts.get("price_entity", ""), 50.0),
            target_kw=self._coord.target_kw,
            ellevio_weight=weight,
            headroom_kw=headroom,
        )

        # ── Plan Executor decides ───────────────────────────────
        ev_phase = int(opts.get("ev_phase_count", 3))
        exec_cfg = ExecutorConfig(
            ev_phase_count=ev_phase,
            ev_min_amps=int(opts.get("ev_min_amps", DEFAULT_EV_MIN_AMPS)),
            ev_max_amps=int(opts.get("ev_max_amps", DEFAULT_EV_MAX_AMPS)),
            grid_charge_price_threshold=float(opts.get("grid_charge_price_threshold", 15.0)),
        )
        cmd = execute_plan_hour(plan_action, exec_state, exec_cfg)

        _LOGGER.debug(
            "V2 EXEC: bat=%s %dW, ev=%s %dA, reason=%s",
            cmd.battery_action,
            cmd.battery_discharge_w,
            cmd.ev_action,
            cmd.ev_amps,
            cmd.reason,
        )

        # Track desired battery action for EMS enforcement (PLAT-1099)
        self._coord._last_battery_action = cmd.battery_action

        # ── Execute battery command ─────────────────────────────
        if cmd.battery_action == "discharge" and cmd.battery_discharge_w > 0:
            bat1_kwh = float(opts.get("battery_1_kwh", 15.0))
            bat2_kwh = float(opts.get("battery_2_kwh", 5.0))
            min_soc = float(opts.get("battery_min_soc", 15.0))
            temp1 = getattr(state, "battery_min_cell_temp_1", 15.0) or 15.0
            temp2 = getattr(state, "battery_min_cell_temp_2", 15.0) or 15.0

            adapters = self._coord.inverter_adapters
            max_disch_1 = adapters[0].max_discharge_w if len(adapters) > 0 else 5000
            max_disch_2 = adapters[1].max_discharge_w if len(adapters) > 1 else 5000
            bats = [
                BatteryInfo(
                    "kontor",
                    state.battery_soc_1,
                    bat1_kwh,
                    temp1,
                    min_soc=min_soc,
                    max_discharge_w=max_disch_1 or 5000,
                ),
                BatteryInfo(
                    "forrad",
                    state.battery_soc_2,
                    bat2_kwh,
                    temp2,
                    min_soc=min_soc,
                    max_discharge_w=max_disch_2 or 5000,
                ),
            ]
            bal = calculate_proportional_discharge(bats, cmd.battery_discharge_w)

            adapters = self._coord.inverter_adapters
            for i, alloc in enumerate(bal.allocations):
                if i < len(adapters) and alloc.watts > 50:
                    await adapters[i].set_ems_mode("discharge_pv")
                    await adapters[i].set_fast_charging(on=False)
                    await self._coord.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": (f"number.goodwe_{adapters[i].prefix}_ems_power_limit"),
                            "value": alloc.watts,
                        },
                    )
                elif i < len(adapters):
                    await adapters[i].set_ems_mode("battery_standby")
                    await adapters[i].set_fast_charging(on=False)

        elif cmd.battery_action == "charge_pv":
            for adapter in self._coord.inverter_adapters:
                await adapter.set_ems_mode("charge_pv")
                await adapter.set_fast_charging(on=False)
                # PLAT-1099: Zero ems_power_limit immediately — defense-in-depth.
                with contextlib.suppress(Exception):
                    await self._coord.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": (f"number.goodwe_{adapter.prefix}_ems_power_limit"),
                            "value": 0,
                        },
                    )

        elif cmd.battery_action == "grid_charge":
            self._coord._fast_charge_authorized = True
            for adapter in self._coord.inverter_adapters:
                await adapter.set_ems_mode("charge_pv")
                await adapter.set_fast_charging(
                    on=True,
                    power_pct=100,
                    soc_target=100,
                    authorized=True,
                )

        elif cmd.battery_action == "standby":
            for adapter in self._coord.inverter_adapters:
                await adapter.set_ems_mode("battery_standby")
                await adapter.set_fast_charging(on=False)

        # PLAT-1099: EMS enforcement runs EVERY cycle, even when Grid Guard acts.
        await self.enforce_ems_modes()

        # P0-FIX: Record decision so sensor.carma_box_decision updates
        await self._coord._record_decision(
            state,
            action=cmd.battery_action,
            reason=cmd.reason,
            discharge_w=cmd.battery_discharge_w,
        )

        # Discharge drift-guard: detect when battery doesn't deliver
        self._coord._check_discharge_drift(state, cmd)

        # ── Natt-EV-workflow ────────────────────────────────────
        if self._coord.ev_adapter:
            plug_state = self._coord.hass.states.get(
                f"binary_sensor.{self._coord.ev_adapter.prefix}_plug"
            )
            if plug_state and plug_state.state == "on":
                await self._coord.ev_adapter.ensure_initialized(force=True)

        ev_connected = (
            (self._coord.ev_adapter and self._coord.ev_adapter.cable_locked)
            if self._coord.ev_adapter
            else False
        )
        ev_soc = state.ev_soc if state.ev_soc >= 0 else -1
        ev_target = float(opts.get("ev_night_target_soc", DEFAULT_EV_NIGHT_TARGET_SOC))
        ev_departure = int(opts.get("ev_departure_hour", DEFAULT_NIGHT_END))

        if not hasattr(self._coord, "_night_ev_active"):
            self._coord._night_ev_active = False

        # ── Price-aware discharge ───────────────────────────────
        try:
            from .planner import should_discharge_now

            nordpool = self._coord.hass.states.get(
                opts.get("price_entity", "sensor.nordpool_kwh_se3_sek_3_10_025")
            )
            if nordpool:
                today_prices = nordpool.attributes.get("today", [])
                tomorrow_prices = nordpool.attributes.get("tomorrow", [])
                upcoming = [float(p) for p in (today_prices[hour:] + tomorrow_prices) if p][:24]
                current_price = (
                    float(nordpool.state)
                    if nordpool.state not in ["unknown", "unavailable"]
                    else 50.0
                )

                discharge_decision = should_discharge_now(
                    current_price_ore=current_price,
                    upcoming_prices_ore=upcoming,
                    battery_soc_pct=state.total_battery_soc,
                )
                if discharge_decision.get("discharge") and not self._coord._night_ev_active:
                    _LOGGER.info(
                        "PRICE-DISCHARGE: %s (%.0f öre, avg_exp %.0f)",
                        discharge_decision.get("reason", "")[:60],
                        current_price,
                        discharge_decision.get("avg_expensive", 0),
                    )
                    rate_kw = discharge_decision.get("recommended_kw", 2.0)
                    rate_w = int(rate_kw * 1000)
                    for adapter in self._coord.inverter_adapters:
                        await adapter.set_ems_mode("discharge_pv")
                    ps_limit = max(500, int(2500 - rate_w / 2))
                    for adapter in self._coord.inverter_adapters:
                        if hasattr(adapter, "device_id") and adapter.device_id:
                            with contextlib.suppress(Exception):
                                await self._coord.hass.services.async_call(
                                    "goodwe",
                                    "set_parameter",
                                    {
                                        "device_id": adapter.device_id,
                                        "parameter": "peak_shaving_power_limit",
                                        "value": ps_limit,
                                    },
                                )
                    _LOGGER.info(
                        "PRICE-DISCHARGE: rate=%.1fkW PS=%dW",
                        rate_kw,
                        ps_limit,
                    )
                    self._coord._price_discharge_active = True
                    self._coord._last_battery_action = "discharge"
                elif getattr(self._coord, "_price_discharge_active", False) and not (
                    discharge_decision.get("discharge")
                ):
                    _LOGGER.info("PRICE-DISCHARGE: Stopped — price no longer profitable")
                    for adapter in self._coord.inverter_adapters:
                        await adapter.set_ems_mode("charge_pv")
                    self._coord._price_discharge_active = False
                    self._coord._last_battery_action = "charge_pv"
        except Exception:
            _LOGGER.debug("Price-discharge check failed", exc_info=True)

        # ── EV timing: charge tonight or wait? ──
        _ev_charge_tonight = True
        if (
            is_night
            and ev_connected
            and 0 <= ev_soc < ev_target
            and not self._coord._night_ev_active
        ):
            try:
                from .planner import should_charge_ev_tonight

                nordpool = self._coord.hass.states.get(
                    opts.get("price_entity", "sensor.nordpool_kwh_se3_sek_3_10_025")
                )
                if nordpool:
                    today_p = nordpool.attributes.get("today", [])
                    tomorrow_p = nordpool.attributes.get("tomorrow", [])
                    tonight = [float(p) for p in today_p[22:] + today_p[:6] if p]
                    tmr_night = (
                        [float(p) for p in tomorrow_p[22:] + tomorrow_p[:6] if p]
                        if tomorrow_p
                        else []
                    )
                    pv_tmr = float(
                        self._coord.hass.states.get(
                            "sensor.solcast_pv_forecast_forecast_tomorrow",
                            type("", (), {"state": "0"})(),
                        ).state
                    )
                    from datetime import datetime as _dt

                    _tomorrow_weekday = (_dt.now().weekday() + 1) % 7
                    _is_workday = _tomorrow_weekday < 5

                    ev_timing = should_charge_ev_tonight(
                        ev_soc_pct=ev_soc,
                        ev_target_pct=ev_target,
                        ev_cap_kwh=float(opts.get("ev_capacity_kwh", 92)),
                        tonight_prices_ore=tonight,
                        tomorrow_night_prices_ore=tmr_night,
                        pv_tomorrow_kwh=pv_tmr,
                        is_workday_tomorrow=_is_workday,
                    )
                    _ev_charge_tonight = ev_timing.get("charge", True)
                    if not _ev_charge_tonight:
                        _LOGGER.info(
                            "EV-TIMING: Skip tonight — %s",
                            ev_timing.get("reason", "")[:60],
                        )
            except Exception:
                _LOGGER.debug("EV timing check failed", exc_info=True)

        if (
            is_night
            and ev_connected
            and 0 <= ev_soc < ev_target
            and not self._coord._night_ev_active
            and _ev_charge_tonight
        ):
            ev_kw = 230 * ev_phase * DEFAULT_EV_MIN_AMPS / 1000
            house_kw = max(0, state.grid_power_w) / 1000
            grid_max = float(opts.get("ellevio_tak_kw", 2.0)) / 0.5
            bat_support_needed = max(0, ev_kw + house_kw - grid_max)

            _LOGGER.info(
                "NATT-EV: SoC %.0f%% < target %.0f%%, starting EV 6A + urladdning %.0fW",
                ev_soc,
                ev_target,
                bat_support_needed * 1000,
            )
            try:
                await self._coord.hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": "button.easee_home_12840_override_schedule"},
                )
            except Exception:
                _LOGGER.warning("NATT-EV: override_schedule misslyckades")
            await self.cmd_ev_start(DEFAULT_EV_MIN_AMPS)
            self._coord._night_ev_active = True

            if bat_support_needed > 0.1:
                bat1_kwh = float(opts.get("battery_1_kwh", 15.0))
                bat2_kwh = float(opts.get("battery_2_kwh", 5.0))
                min_soc_val = float(opts.get("battery_min_soc", 15.0))
                temp1 = getattr(state, "battery_min_cell_temp_1", 15.0) or 15.0
                temp2 = getattr(state, "battery_min_cell_temp_2", 15.0) or 15.0
                _adapters = self._coord.inverter_adapters
                _max_d1 = _adapters[0].max_discharge_w if len(_adapters) > 0 else 5000
                _max_d2 = _adapters[1].max_discharge_w if len(_adapters) > 1 else 5000
                bats = [
                    BatteryInfo(
                        "kontor",
                        state.battery_soc_1,
                        bat1_kwh,
                        temp1,
                        min_soc=min_soc_val,
                        max_discharge_w=_max_d1 or 5000,
                    ),
                    BatteryInfo(
                        "forrad",
                        state.battery_soc_2,
                        bat2_kwh,
                        temp2,
                        min_soc=min_soc_val,
                        max_discharge_w=_max_d2 or 5000,
                    ),
                ]
                bal = calculate_proportional_discharge(
                    bats,
                    int(bat_support_needed * 1000),
                )
                adapters = self._coord.inverter_adapters
                for i, alloc in enumerate(bal.allocations):
                    if i < len(adapters) and alloc.watts > 50:
                        await adapters[i].set_ems_mode("discharge_pv")
                        await adapters[i].set_fast_charging(on=False)
                        await self._coord.hass.services.async_call(
                            "number",
                            "set_value",
                            {
                                "entity_id": (
                                    f"number.goodwe_{adapters[i].prefix}_ems_power_limit"
                                ),
                                "value": alloc.watts,
                            },
                        )

        # Stopp EV vid departure hour eller target nådd
        if self._coord._night_ev_active and (
            now.hour == ev_departure or (ev_soc >= 0 and ev_soc >= ev_target) or not is_night
        ):
            _LOGGER.info("NATT-EV: Stoppar EV (SoC=%.0f%%, hour=%d)", ev_soc, now.hour)
            await self.cmd_ev_stop()
            self._coord._night_ev_active = False

        # ── Execute EV command (from plan) — SKIP if night EV active ──
        if self._coord._night_ev_active:
            pass
        elif cmd.ev_action == "start" and cmd.ev_amps >= DEFAULT_EV_MIN_AMPS:
            if not self._coord._ev_enabled:
                await self.cmd_ev_start(cmd.ev_amps)
            elif cmd.ev_amps != self._coord._ev_current_amps:
                await self.cmd_ev_adjust(cmd.ev_amps)
        elif cmd.ev_action == "stop" and self._coord._ev_enabled:
            await self.cmd_ev_stop()

        # ── EXP-12: Real-time PV Surplus Allocation ──────────────────
        if not is_night and not self._coord._night_ev_active and state.pv_power_w > 200:
            try:
                from .planner import allocate_pv_surplus, calculate_pv_confidence

                solcast_today = self._coord.hass.states.get(
                    "sensor.solcast_pv_forecast_forecast_today"
                )
                hourly_pv_remaining: list[float] = []
                if solcast_today:
                    detail = solcast_today.attributes.get("detailedForecast", [])
                    for j in range(0, len(detail), 2):
                        kw = detail[j].get("pv_estimate", 0) if isinstance(detail[j], dict) else 0
                        hourly_pv_remaining.append(float(kw))
                if len(hourly_pv_remaining) > hour:
                    hourly_pv_remaining = hourly_pv_remaining[hour:]

                pv_conf = 1.0
                if self._coord.weather_adapter:
                    try:
                        solcast_current = hourly_pv_remaining[0] if hourly_pv_remaining else 0
                        pv_conf = calculate_pv_confidence(
                            pressure_mbar=self._coord.weather_adapter.pressure_mbar,
                            solar_radiation_wm2=(self._coord.weather_adapter.solar_radiation_wm2),
                            solcast_estimate_kw=solcast_current,
                            hour=hour,
                        )
                    except Exception:
                        _LOGGER.debug("Could not add weather sample to predictor", exc_info=True)

                is_workday = now.weekday() < 5
                sunset_h = 19 if now.month in (3, 4, 5, 6, 7, 8, 9) else 16
                hours_to_sunset = max(0, sunset_h - hour)

                bat_max_charge = 5000
                if self._coord.inverter_adapters:
                    bat_max_charge = sum(
                        a.max_charge_w or 5000 for a in self._coord.inverter_adapters
                    )

                pv_alloc = allocate_pv_surplus(
                    pv_now_w=state.pv_power_w,
                    grid_now_w=state.grid_power_w,
                    house_consumption_w=max(500, state.grid_power_w + state.pv_power_w),
                    battery_soc_pct=state.total_battery_soc,
                    battery_cap_kwh=(
                        float(opts.get("battery_1_kwh", 15)) + float(opts.get("battery_2_kwh", 5))
                    ),
                    ev_soc_pct=float(ev_soc) if ev_soc >= 0 else -1,
                    ev_connected=ev_connected,
                    ev_target_pct=float(opts.get("ev_target_soc", 75)),
                    is_workday=is_workday,
                    hours_to_sunset=hours_to_sunset,
                    hourly_pv_remaining_kw=hourly_pv_remaining,
                    pv_confidence=pv_conf,
                    battery_max_charge_w=bat_max_charge,
                )

                self._coord._pv_allocation = {
                    "timestamp": now.isoformat(),
                    "ev_action": pv_alloc.ev_action,
                    "ev_amps": pv_alloc.ev_amps,
                    "battery_action": pv_alloc.battery_action,
                    "battery_target_w": pv_alloc.battery_target_w,
                    "consumers_action": pv_alloc.consumers_action,
                    "surplus_w": round(pv_alloc.surplus_w),
                    "will_export": pv_alloc.will_export,
                    "reason": pv_alloc.reason,
                    "is_workday": is_workday,
                    "pv_confidence": round(pv_conf, 2),
                }

                if pv_alloc.ev_action == "charge" and pv_alloc.ev_amps >= DEFAULT_EV_MIN_AMPS:
                    if not self._coord._ev_enabled:
                        _LOGGER.info(
                            "SOLAR-EV: start %dA (surplus %.0fW, bat %.0f%%)",
                            pv_alloc.ev_amps,
                            pv_alloc.surplus_w,
                            state.total_battery_soc,
                        )
                        await self.cmd_ev_start(pv_alloc.ev_amps)
                    elif pv_alloc.ev_amps != self._coord._ev_current_amps:
                        await self.cmd_ev_adjust(pv_alloc.ev_amps)
                elif (
                    pv_alloc.ev_action != "charge"
                    and self._coord._ev_enabled
                    and not self._coord._night_ev_active
                ):
                    _LOGGER.info("SOLAR-EV: stop (reason: %s)", pv_alloc.reason)
                    await self.cmd_ev_stop()

                if self._coord.ev_adapter:
                    disc_alert = self._coord.ev_adapter.check_unexpected_disconnect(
                        was_charging=(self._coord._ev_enabled and self._coord._ev_current_amps > 0)
                    )
                    if disc_alert:
                        _LOGGER.warning("EV-ALERT: %s", disc_alert)

                if (
                    self._coord.ev_adapter
                    and ev_connected
                    and self._coord.ev_adapter.needs_recovery
                ):
                    recovery = await self._coord.ev_adapter.try_recover()
                    if recovery:
                        _LOGGER.warning("EV-RECOVERY: %s", recovery)

            except Exception:
                _LOGGER.debug("SOLAR-EV: allocation failed", exc_info=True)

        # ── Surplus chain — ALWAYS runs ──────────────────────────
        if not hasattr(self._coord, "_surplus_hysteresis"):
            from .surplus_chain import HysteresisState

            self._coord._surplus_hysteresis = HysteresisState()

        consumers = self._coord._build_surplus_consumers(state)
        surplus_cfg = SurplusConfig(start_delay_s=60, stop_delay_s=180)

        if state.grid_power_w < -100:
            surplus_w = abs(state.grid_power_w)
            result = allocate_surplus(
                surplus_w,
                consumers,
                self._coord._surplus_hysteresis,
                surplus_cfg,
            )
            await self.execute_surplus_allocations(result.allocations)
        elif state.grid_power_w > 100:
            is_night_now = now.hour >= DEFAULT_NIGHT_START or now.hour < DEFAULT_NIGHT_END
            weight_now = DEFAULT_NIGHT_WEIGHT if is_night_now else 1.0
            viktat_kw = max(0, state.grid_power_w) / 1000 * weight_now
            if viktat_kw > self._coord.target_kw * 1.05:
                deficit_w = (viktat_kw - self._coord.target_kw) / weight_now * 1000
                reductions = should_reduce_consumers(
                    deficit_w,
                    consumers,
                    self._coord._surplus_hysteresis,
                    surplus_cfg,
                )
                await self.execute_surplus_allocations(reductions)

        # ── Replan check ────────────────────────────────────────
        if not hasattr(self._coord, "_replan_deviation_count"):
            self._coord._replan_deviation_count = 0

        needs_replan, self._coord._replan_deviation_count = check_replan_needed(
            plan_action,
            exec_state,
            self._coord._replan_deviation_count,
        )
        if needs_replan:
            _LOGGER.info("V2 EXEC: Avvikelse → omplanering")
            self._coord._plan_counter = PLAN_INTERVAL_SECONDS // SCAN_INTERVAL_SECONDS

    # ═══════════════════════════════════════════════════════════════════════
    # execute_surplus_allocations
    # ═══════════════════════════════════════════════════════════════════════

    async def execute_surplus_allocations(self, allocations: list[SurplusAllocation]) -> None:
        """Utför surplus chain-allokeringar mot hårdvara.

        Args:
            allocations: Lista av SurplusAllocation från surplus_chain.
        """
        for alloc in allocations:
            if alloc.action == "none":
                continue
            try:
                if alloc.id == "miner":
                    if alloc.action == "start":
                        await self._coord.hass.services.async_call(
                            "switch",
                            "turn_on",
                            {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"},
                        )
                    elif alloc.action == "stop":
                        await self._coord.hass.services.async_call(
                            "switch",
                            "turn_off",
                            {"entity_id": "switch.shelly1pmg4_a085e3bd1e60"},
                        )
                elif alloc.id == "ev":
                    if alloc.action == "start" and alloc.target_w >= 4140:
                        amps = int(alloc.target_w / (230 * 3))
                        clamped = max(DEFAULT_EV_MIN_AMPS, min(DEFAULT_EV_MAX_AMPS, amps))
                        await self.cmd_ev_start(clamped)
                    elif alloc.action == "increase":
                        amps = int(alloc.target_w / (230 * 3))
                        clamped = max(DEFAULT_EV_MIN_AMPS, min(DEFAULT_EV_MAX_AMPS, amps))
                        await self.cmd_ev_adjust(clamped)
                    elif alloc.action == "stop":
                        await self.cmd_ev_stop()
                elif alloc.id == "battery" and alloc.action in ("start", "increase"):
                    charge_w = int(alloc.target_w) if alloc.target_w else 3000
                    charge_pct = min(100, max(10, int(charge_w / 60)))
                    for adapter in self._coord.inverter_adapters:
                        await adapter.set_ems_mode("charge_pv")
                        await adapter.set_fast_charging(
                            on=True,
                            power_pct=charge_pct,
                            soc_target=100,
                            authorized=True,
                        )
                elif alloc.id == "battery" and alloc.action in ("stop", "decrease"):
                    for adapter in self._coord.inverter_adapters:
                        await adapter.set_fast_charging(on=False)
            except Exception as err:
                _LOGGER.error("Surplus allocation %s failed: %s", alloc.id, err)

    # ═══════════════════════════════════════════════════════════════════════
    # enforce_ems_modes — PLAT-1099
    # ═══════════════════════════════════════════════════════════════════════

    async def enforce_ems_modes(self) -> None:
        """Enforce EMS-läge på ALLA invertrar varje cykel (PLAT-1099).

        1. Mappa _last_battery_action → önskat EMS-läge
        2. Per inverter: kontrollera faktiskt läge, applicera om drift
        3. Nollställ ems_power_limit vid charge_pv (PLAT-1040)
        4. Detektera INV-2 crosscharge på EMS-nivå
        """
        if not self._coord.inverter_adapters:
            return

        battery_action = getattr(self._coord, "_last_battery_action", "charge_pv")

        ems_mode_map = {
            "charge_pv": "charge_pv",
            "grid_charge": "charge_pv",
            "discharge": "discharge_pv",
            "standby": "battery_standby",
        }
        desired_ems = ems_mode_map.get(battery_action, "charge_pv")

        # INV-3: fast_charging MÅSTE vara AV vid urladdning
        if desired_ems == "discharge_pv":
            for _adp in self._coord.inverter_adapters:
                fc_entity = f"switch.goodwe_fast_charging_switch_{_adp.prefix}"
                fc_state = self._coord.hass.states.get(fc_entity)
                if fc_state and fc_state.state == "on":
                    _LOGGER.warning(
                        "INV-3 ENFORCE: %s fast_charging ON during discharge → OFF",
                        _adp.prefix,
                    )
                    await _adp.set_fast_charging(on=False)

        # Track vad varje adapter SKA vara efter enforcement (för INV-2-kontroll).
        enforced_modes: list[str] = []

        for _idx, _adp in enumerate(self._coord.inverter_adapters):
            # --- PLAT-1040: ems_power_limit MÅSTE vara 0 vid charge_pv ---
            if desired_ems == "charge_pv":
                try:
                    _lim_entity = f"number.goodwe_{_adp.prefix}_ems_power_limit"
                    _lim_state = self._coord.hass.states.get(_lim_entity)
                    _lim_val = (
                        float(_lim_state.state)
                        if _lim_state and _lim_state.state not in ("unknown", "unavailable")
                        else 0.0
                    )
                    if _lim_val != 0.0:
                        _LOGGER.info(
                            "EMS PLAT-1040: %s limit %.0f→0",
                            _adp.prefix,
                            _lim_val,
                        )
                        await self._coord.hass.services.async_call(
                            "number",
                            "set_value",
                            {"entity_id": _lim_entity, "value": 0},
                        )
                except Exception:
                    _LOGGER.warning(
                        "EMS ENFORCE: %s failed to reset ems_power_limit",
                        _adp.prefix,
                    )

            # --- Drift-korrigering: applicera läge om faktiskt != önskat ---
            _current_ems = _adp.ems_mode
            if _current_ems and _current_ems != desired_ems:
                # Under urladdning är battery_standby på enskilt batteri OK (alloc=0W).
                if battery_action == "discharge" and _current_ems == "battery_standby":
                    enforced_modes.append("battery_standby")
                    continue
                _LOGGER.info(
                    "EMS ENFORCE: %s drift detected %s → %s (action=%s)",
                    _adp.prefix,
                    _current_ems,
                    desired_ems,
                    battery_action,
                )
                await _adp.set_ems_mode(desired_ems)
                enforced_modes.append(desired_ems)
            else:
                enforced_modes.append(_current_ems or desired_ems)

        # ── INV-2 Crosscharge Prevention — EMS-nivå ─────────────
        has_crosscharge = (
            len(enforced_modes) >= 2
            and "discharge_pv" in enforced_modes
            and "charge_pv" in enforced_modes
        )
        if has_crosscharge:
            _LOGGER.error(
                "INV-2 CROSSCHARGE EMS: %s — forcing all to charge_pv",
                enforced_modes,
            )
            for _adp in self._coord.inverter_adapters:
                await _adp.set_ems_mode("charge_pv")
                await _adp.set_fast_charging(on=False)
                with contextlib.suppress(Exception):
                    await self._coord.hass.services.async_call(
                        "number",
                        "set_value",
                        {
                            "entity_id": (f"number.goodwe_{_adp.prefix}_ems_power_limit"),
                            "value": 0,
                        },
                    )

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_miner
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_miner(self, on: bool) -> None:
        """Sätt miner-switch på/av.

        Args:
            on: True = starta, False = stäng av.
        """
        if not self._coord._miner_entity:
            return
        service = "turn_on" if on else "turn_off"
        _LOGGER.info("CARMA: miner %s → %s", self._coord._miner_entity, service)
        try:
            await self._coord.hass.services.async_call(
                "switch",
                service,
                {"entity_id": self._coord._miner_entity},
            )
            self._coord._miner_on = on
            await self._coord._async_save_runtime()
        except Exception:
            _LOGGER.warning("CARMA: miner control failed", exc_info=True)

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_ev_start
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_ev_start(self, amps: int = DEFAULT_EV_MIN_AMPS) -> None:
        """Starta EV: sätt ström FÖRST, aktivera sedan (förhindrar 16A-burst).

        Args:
            amps: Laddström i ampere (kläms till [DEFAULT_EV_MIN_AMPS, DEFAULT_EV_MAX_AMPS]).
        """
        amps = max(DEFAULT_EV_MIN_AMPS, min(amps, DEFAULT_EV_MAX_AMPS))
        if self._coord._ev_enabled and self._coord._ev_current_amps == amps:
            return
        if not self._coord.ev_adapter:
            return
        _LOGGER.info("CARMA: EV start %dA", amps)
        ok = await self._coord.ev_adapter.set_current(amps)
        if not ok:
            return
        with contextlib.suppress(Exception):
            await self._coord.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": (f"number.{self._coord.ev_adapter.prefix}_dynamic_charger_limit"),
                    "value": amps,
                },
            )
        if not self._coord._ev_enabled:
            ok = await self._coord.ev_adapter.enable()
            if not ok:
                await self._coord.ev_adapter.disable()
                return
        self._coord._ev_enabled = True
        self._coord._ev_current_amps = amps
        await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_ev_stop
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_ev_stop(self) -> None:
        """Stoppa EV: inaktivera och återställ till minström."""
        if not self._coord.ev_adapter:
            return
        _LOGGER.info("CARMA: EV stop")
        await self._coord.ev_adapter.disable()
        await self._coord.ev_adapter.reset_to_default()
        with contextlib.suppress(Exception):
            await self._coord.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": (f"number.{self._coord.ev_adapter.prefix}_dynamic_charger_limit"),
                    "value": DEFAULT_EV_MIN_AMPS,
                },
            )
        self._coord._ev_enabled = False
        self._coord._ev_current_amps = 0
        await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_ev_adjust
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_ev_adjust(self, amps: int) -> None:
        """Justera EV-ström utan enable/disable.

        EXP-04: Rampa UPP ett steg i taget (6→8→10).
        Rampa NED direkt till mål (säkert, ingen surge-risk).

        Args:
            amps: Målström i ampere.
        """
        if not self._coord.ev_adapter or not self._coord._ev_enabled:
            return
        amps = max(DEFAULT_EV_MIN_AMPS, min(amps, DEFAULT_EV_MAX_AMPS))
        if amps == self._coord._ev_current_amps:
            return
        if amps > self._coord._ev_current_amps:
            next_step = amps
            for step in EV_RAMP_STEPS:
                if step > self._coord._ev_current_amps:
                    next_step = min(step, amps)
                    break
            amps = next_step
        _LOGGER.info("CARMA: EV adjust %dA -> %dA", self._coord._ev_current_amps, amps)
        ok = await self._coord.ev_adapter.set_current(amps)
        with contextlib.suppress(Exception):
            await self._coord.hass.services.async_call(
                "number",
                "set_value",
                {
                    "entity_id": (f"number.{self._coord.ev_adapter.prefix}_dynamic_charger_limit"),
                    "value": amps,
                },
            )
        if ok:
            self._coord._ev_current_amps = amps
            await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_charge_pv
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_charge_pv(self, state: CarmaboxState) -> None:
        """Ladda batterier från sol.

        SafetyGuard: heartbeat + rate limit + charge check.

        Args:
            state: Aktuellt systemtillstånd.
        """
        from ..optimizer.models import BatteryCommand

        if self._coord._last_command in (
            BatteryCommand.CHARGE_PV,
            BatteryCommand.CHARGE_PV_TAPER,
        ):
            return

        heartbeat = self._coord.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked charge_pv: %s", heartbeat.reason)
            self._coord._daily_safety_blocks += 1
            return

        rate = self._coord.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked charge_pv: %s", rate.reason)
            self._coord._daily_safety_blocks += 1
            return

        temp_c = self._coord._read_battery_temp()
        charge_check = self._coord.safety.check_charge(
            state.battery_soc_1, state.battery_soc_2, temp_c
        )
        if not charge_check.ok:
            _LOGGER.info("SafetyGuard blocked charge_pv: %s", charge_check.reason)
            self._coord._daily_safety_blocks += 1
            return

        _LOGGER.info("CARMA: charge_pv (solar surplus)")
        success = False
        failed = False

        if self._coord.inverter_adapters:
            for adapter in self._coord.inverter_adapters:
                if adapter.soc >= 100:
                    ok = await adapter.set_ems_mode("battery_standby")
                else:
                    ok = await adapter.set_ems_mode("charge_pv")
                    if ok and isinstance(adapter, GoodWeAdapter):
                        await adapter.set_fast_charging(on=False)
                if ok:
                    success = True
                else:
                    failed = True

            if failed and success:
                _LOGGER.warning("Partial charge_pv failure — rolling back all to standby")
                for adapter in self._coord.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                self._coord._daily_safety_blocks += 1
                success = False
        else:
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._coord._get_entity(ems_key)
                if not entity:
                    continue
                soc_key = ems_key.replace("ems", "soc")
                soc = self._coord._read_float(self._coord._get_entity(soc_key))
                mode = "battery_standby" if soc >= 100 else "charge_pv"
                if await self._coord._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": mode}
                ):
                    if self._coord.executor_enabled:
                        self._coord._check_write_verify(entity, mode)
                    success = True
                else:
                    failed = True

            if failed and success:
                _LOGGER.warning("Partial charge_pv failure — rolling back all to standby (legacy)")
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._coord._get_entity(ems_key)
                    if entity:
                        await self._coord._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._coord._daily_safety_blocks += 1
                success = False

        if success:
            self._coord._last_command = BatteryCommand.CHARGE_PV
            self._coord.safety.record_mode_change()
            await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_grid_charge
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_grid_charge(self, state: CarmaboxState) -> None:
        """Ladda batterier från elnätet (billigt pris).

        GoodWe: charge_pv + fast_charging = laddar från grid när ingen PV.
        SafetyGuard: heartbeat + rate limit + charge check.

        Args:
            state: Aktuellt systemtillstånd.
        """
        from ..optimizer.models import BatteryCommand

        if self._coord._last_command == BatteryCommand.CHARGE_PV:
            if self._coord.inverter_adapters:
                for adapter in self._coord.inverter_adapters:
                    if isinstance(adapter, GoodWeAdapter) and adapter.soc < 100:
                        await adapter.set_fast_charging(on=True, power_pct=100, soc_target=100)
            return

        heartbeat = self._coord.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked grid_charge: %s", heartbeat.reason)
            self._coord._daily_safety_blocks += 1
            return

        rate = self._coord.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked grid_charge: %s", rate.reason)
            self._coord._daily_safety_blocks += 1
            return

        temp_c = self._coord._read_battery_temp()
        charge_check = self._coord.safety.check_charge(
            state.battery_soc_1, state.battery_soc_2, temp_c
        )
        if not charge_check.ok:
            _LOGGER.info("SafetyGuard blocked grid_charge: %s", charge_check.reason)
            self._coord._daily_safety_blocks += 1
            return

        _LOGGER.info("CARMA: grid_charge (cheap price)")
        success = False
        failed = False

        if self._coord.inverter_adapters:
            for adapter in self._coord.inverter_adapters:
                if adapter.soc >= 100:
                    ok = await adapter.set_ems_mode("battery_standby")
                else:
                    ok = await adapter.set_ems_mode("charge_pv")
                    if ok and hasattr(adapter, "set_fast_charging"):
                        ok = await adapter.set_fast_charging(on=True, power_pct=100, soc_target=100)
                if ok:
                    success = True
                else:
                    failed = True

            if failed and success:
                _LOGGER.warning("Partial grid_charge failure — rolling back all to standby")
                for adapter in self._coord.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                    if hasattr(adapter, "set_fast_charging"):
                        await adapter.set_fast_charging(on=False, power_pct=0, soc_target=100)
                self._coord._daily_safety_blocks += 1
                success = False
        else:
            _LOGGER.warning(
                "Grid charge requested but no GoodWe adapter — using charge_pv (may not work)"
            )
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._coord._get_entity(ems_key)
                if not entity:
                    continue
                soc_key = ems_key.replace("ems", "soc")
                soc = self._coord._read_float(self._coord._get_entity(soc_key))
                mode = "battery_standby" if soc >= 100 else "charge_pv"
                if await self._coord._safe_service_call(
                    "select", "select_option", {"entity_id": entity, "option": mode}
                ):
                    if self._coord.executor_enabled:
                        self._coord._check_write_verify(entity, mode)
                    success = True
                else:
                    failed = True

            if failed and success:
                _LOGGER.warning(
                    "Partial grid_charge failure — rolling back all to standby (legacy)"
                )
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._coord._get_entity(ems_key)
                    if entity:
                        await self._coord._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._coord._daily_safety_blocks += 1
                success = False

        if success:
            self._coord._last_command = BatteryCommand.CHARGE_PV
            self._coord.safety.record_mode_change()
            await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_standby
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_standby(self, state: CarmaboxState, force: bool = False) -> None:
        """Sätt alla batterier i standby.

        SafetyGuard: heartbeat + rate limit (hoppas över om force=True
        eftersom forcerad standby i sig är en säkerhetsåtgärd).

        Args:
            state: Aktuellt systemtillstånd.
            force: Om True, hoppa över idempotens- och säkerhetsgate.
        """
        from ..optimizer.models import BatteryCommand

        if not force and self._coord._last_command == BatteryCommand.STANDBY:
            return

        if not force:
            heartbeat = self._coord.safety.check_heartbeat()
            if not heartbeat.ok:
                _LOGGER.warning("SafetyGuard blocked standby: %s", heartbeat.reason)
                self._coord._daily_safety_blocks += 1
                return

            rate = self._coord.safety.check_rate_limit()
            if not rate.ok:
                _LOGGER.info("SafetyGuard blocked standby: %s", rate.reason)
                self._coord._daily_safety_blocks += 1
                return

        _LOGGER.info("CARMA: standby%s", " (forced)" if force else "")
        success = False

        if self._coord.inverter_adapters:
            for adapter in self._coord.inverter_adapters:
                ok = await adapter.set_ems_mode("battery_standby")
                if ok:
                    success = True
                    await adapter.set_discharge_limit(0)
                    if isinstance(adapter, GoodWeAdapter):
                        await adapter.set_fast_charging(on=False)
        else:
            for ems_key in ("battery_ems_1", "battery_ems_2"):
                entity = self._coord._get_entity(ems_key)
                if entity and await self._coord._safe_service_call(
                    "select",
                    "select_option",
                    {"entity_id": entity, "option": "battery_standby"},
                ):
                    if self._coord.executor_enabled:
                        self._coord._check_write_verify(entity, "battery_standby")
                    success = True

        if success:
            self._coord._last_command = BatteryCommand.STANDBY
            self._coord.safety.record_mode_change()
            await self._coord._async_save_runtime()

    # ═══════════════════════════════════════════════════════════════════════
    # cmd_discharge
    # ═══════════════════════════════════════════════════════════════════════

    async def cmd_discharge(self, state: CarmaboxState, watts: int) -> None:
        """Urladda batterier med angiven effekt.

        SafetyGuard: heartbeat + rate limit + discharge check.

        Args:
            state: Aktuellt systemtillstånd.
            watts: Önskad urladdningseffekt i watt.
        """
        from ..optimizer.models import BatteryCommand

        # K1: Hoppa om redan urladdas med liknande effekt (±100W)
        if (
            self._coord._last_command == BatteryCommand.DISCHARGE
            and abs(watts - self._coord._last_discharge_w) < 100
        ):
            _LOGGER.debug(
                "K1: skip redundant discharge (%dW ≈ %dW)",
                watts,
                self._coord._last_discharge_w,
            )
            return

        heartbeat = self._coord.safety.check_heartbeat()
        if not heartbeat.ok:
            _LOGGER.warning("SafetyGuard blocked discharge: %s", heartbeat.reason)
            self._coord._daily_safety_blocks += 1
            return

        rate = self._coord.safety.check_rate_limit()
        if not rate.ok:
            _LOGGER.info("SafetyGuard blocked discharge: %s", rate.reason)
            self._coord._daily_safety_blocks += 1
            return

        temp_c = self._coord._read_battery_temp()
        discharge_check = self._coord.safety.check_discharge(
            state.battery_soc_1,
            state.battery_soc_2,
            self._coord.min_soc,
            state.grid_power_w,
            temp_c,
        )
        if not discharge_check.ok:
            _LOGGER.info("SafetyGuard blocked discharge: %s", discharge_check.reason)
            self._coord._daily_safety_blocks += 1
            return

        cell_temp_k = self._coord._read_cell_temp("kontor")
        cell_temp_f = self._coord._read_cell_temp("forrad")
        cold_lock_temp = float(self._coord._cfg.get("cold_lock_temp_c", 10.0))
        if cell_temp_k is not None and cell_temp_k < cold_lock_temp:
            _LOGGER.debug(
                "CARMA: Kontor %.1f°C < %.0f → discharge OK but charge blocked",
                cell_temp_k,
                cold_lock_temp,
            )
        if cell_temp_f is not None and cell_temp_f < cold_lock_temp:
            _LOGGER.debug(
                "CARMA: Forrad %.1f°C < %.0f → discharge OK but charge blocked",
                cell_temp_f,
                cold_lock_temp,
            )

        _LOGGER.info("CARMA: discharge %dW (target %.1f kW)", watts, self._coord.target_kw)

        if self._coord.inverter_adapters:
            opts = self._coord._cfg
            defaults = [DEFAULT_BATTERY_1_KWH, DEFAULT_BATTERY_2_KWH]
            caps = [
                float(opts.get(f"battery_{i}_kwh", defaults[i - 1]))
                for i in range(1, len(self._coord.inverter_adapters) + 1)
            ]
            stored = [
                max(0, a.soc) * caps[idx] for idx, a in enumerate(self._coord.inverter_adapters)
            ]
            total_soc = sum(stored)
            if total_soc <= 0:
                return

            success = False
            failed = False
            for idx, adapter in enumerate(self._coord.inverter_adapters):
                if stored[idx] <= 0:
                    continue
                ems_ok = await adapter.set_ems_mode("auto")
                if not ems_ok:
                    failed = True
                    continue
                limit_ok = await adapter.set_discharge_limit(0)
                if not limit_ok:
                    _LOGGER.error("Discharge limit failed — rolling back to standby")
                    await adapter.set_ems_mode("battery_standby")
                    failed = True
                    continue
                success = True

            if failed and success:
                _LOGGER.warning("Partial discharge failure — rolling back all to standby")
                for adapter in self._coord.inverter_adapters:
                    await adapter.set_ems_mode("battery_standby")
                self._coord._daily_safety_blocks += 1
                success = False

            if success:
                self._coord._last_command = BatteryCommand.DISCHARGE
                self._coord._last_discharge_w = watts
                self._coord.safety.record_mode_change()
                await self._coord._async_save_runtime()
        else:
            opts = self._coord._cfg
            cap1 = float(opts.get("battery_1_kwh", DEFAULT_BATTERY_1_KWH))
            cap2 = float(opts.get("battery_2_kwh", DEFAULT_BATTERY_2_KWH))
            energy_1 = state.battery_soc_1 * cap1
            energy_2 = max(0, state.battery_soc_2) * cap2
            total_energy = energy_1 + energy_2
            if total_energy <= 0:
                return

            ratio_1 = energy_1 / total_energy
            w1 = int(watts * ratio_1)
            w2 = watts - w1

            success = False
            failed = False
            for ems_key, limit_key, w in [
                ("battery_ems_1", "battery_limit_1", w1),
                ("battery_ems_2", "battery_limit_2", w2),
            ]:
                ems_entity = self._coord._get_entity(ems_key)
                limit_entity = self._coord._get_entity(limit_key)
                if ems_entity and w > 0:
                    ems_ok = await self._coord._safe_service_call(
                        "select",
                        "select_option",
                        {"entity_id": ems_entity, "option": "discharge_battery"},
                    )
                    if not ems_ok:
                        failed = True
                        continue
                    if self._coord.executor_enabled:
                        self._coord._check_write_verify(ems_entity, "discharge_battery")
                    if limit_entity:
                        await self._coord._safe_service_call(
                            "number",
                            "set_value",
                            {"entity_id": limit_entity, "value": w},
                        )
                        success = True

            if failed and success:
                _LOGGER.warning("Partial discharge failure — rolling back all to standby (legacy)")
                for ems_key in ("battery_ems_1", "battery_ems_2"):
                    entity = self._coord._get_entity(ems_key)
                    if entity:
                        await self._coord._safe_service_call(
                            "select",
                            "select_option",
                            {"entity_id": entity, "option": "battery_standby"},
                        )
                self._coord._daily_safety_blocks += 1
                success = False

            if success:
                self._coord._last_command = BatteryCommand.DISCHARGE
                self._coord._last_discharge_w = watts
                self._coord.safety.record_mode_change()
                await self._coord._async_save_runtime()
